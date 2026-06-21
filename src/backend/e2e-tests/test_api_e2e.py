"""API end-to-end tests against a real Klangk server.

Exercises group management, ACL permissions, workspace sharing via ACL,
and permission denials.

Run with: devenv shell -- test-backend-e2e test_api_e2e.py
"""

import os
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx
import pytest

# Env vars from .env that could change test behavior.
_SANITIZED_VARS = [
    "KLANGK_AUTH_MODES",
    "KLANGK_OIDC_CONFIG",
    "KLANGK_DISABLE_REGISTRATION",
    "KLANGK_DISABLE_INVITES",
    "KLANGK_LOGIN_LOCKOUT_FAILURES",
    "KLANGK_MIN_PASSWORD_LENGTH",
    "KLANGK_PREVENT_INSECURE_JWT_SECRET",
]


def _clean_env():
    """Return os.environ with test-affecting vars removed."""
    env = dict(os.environ)
    for var in _SANITIZED_VARS:
        env.pop(var, None)
    return env


def _start_server(data_dir, port, instance_id):
    """Start a Klangk server and wait for it to be ready."""
    env = {
        **_clean_env(),
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "acl-e2e-test-secret",
        "KLANGK_DEFAULT_USER": "admin@example.com",
        "KLANGK_DEFAULT_PASSWORD": "adminpass",
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": instance_id,
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        "KLANGK_PORT_RANGE_START": "9200",
        "LOGFIRE_TOKEN": "",
    }
    proc = subprocess.Popen(
        [
            "uvicorn",
            "klangk_backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--ws-max-size",
            "16777216",
        ],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"Server failed to start:\n{stdout}")
    return proc, base_url


def _stop_server(proc, data_dir, instance_id):
    """Stop a server and clean up."""
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            f"label=klangk.instance={instance_id}",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["podman", "rm", "-f", *result.stdout.strip().split()],
            capture_output=True,
        )
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module."""
    data_dir = tempfile.mkdtemp(prefix="klangk-acl-e2e-")
    proc, base_url = _start_server(data_dir, "18993", "acl-e2e")
    yield {"url": base_url, "data_dir": data_dir, "proc": proc}
    _stop_server(proc, data_dir, "acl-e2e")


def _ws_name(prefix: str) -> str:
    """Generate a unique workspace name to avoid collisions."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def api(server):
    """httpx client pointing at the test server."""
    with httpx.Client(base_url=server["url"], timeout=10.0) as client:
        yield client


def _login(api, email, password):
    """Login and return auth headers."""
    resp = api.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, f"Login failed for {email}: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _register(api, email, password="testpass"):
    """Register a user (test mode) and return auth headers."""
    resp = api.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, f"Register failed for {email}: {resp.text}"
    token = resp.json().get("access_token")
    if token:
        return {"Authorization": f"Bearer {token}"}
    # If registration requires verification, login instead
    return _login(api, email, password)


@pytest.fixture(scope="module")
def admin_headers(api):
    """Auth headers for the default admin user."""
    return _login(api, "admin@example.com", "adminpass")


@pytest.fixture(scope="module")
def user_a(api):
    """Create user A and return (headers, email)."""
    email = "alice@example.com"
    headers = _register(api, email)
    return {"headers": headers, "email": email}


@pytest.fixture(scope="module")
def user_b(api):
    """Create user B and return (headers, email)."""
    email = "bob@example.com"
    headers = _register(api, email)
    return {"headers": headers, "email": email}


# --- Config ---


class TestConfig:
    def test_config_returns_instance_id(self, api):
        resp = api.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["instance_id"] == "acl-e2e"

    def test_config_instance_id_stable_across_requests(self, api):
        resp1 = api.get("/api/config")
        resp2 = api.get("/api/config")
        assert resp1.json()["instance_id"] == resp2.json()["instance_id"]


# --- Group management ---


class TestGroupManagement:
    def test_list_groups(self, api, admin_headers):
        resp = api.get("/admin/groups", headers=admin_headers)
        assert resp.status_code == 200
        groups = resp.json()
        assert any(g["name"] == "admin" for g in groups)

    def test_create_group(self, api, admin_headers):
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "editors", "description": "Can edit stuff"},
        )
        assert resp.status_code == 200
        group = resp.json()
        assert group["name"] == "editors"
        assert group["id"]

    def test_create_duplicate_group_fails(self, api, admin_headers):
        api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "dup-group"},
        )
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "dup-group"},
        )
        assert resp.status_code == 409

    def test_update_group(self, api, admin_headers):
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "to-rename"},
        )
        group_id = resp.json()["id"]
        resp = api.patch(
            f"/admin/groups/{group_id}",
            headers=admin_headers,
            json={"name": "renamed-group", "description": "Updated"},
        )
        assert resp.status_code == 200

    def test_delete_group(self, api, admin_headers):
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "to-delete"},
        )
        group_id = resp.json()["id"]
        resp = api.delete(f"/admin/groups/{group_id}", headers=admin_headers)
        assert resp.status_code == 200
        # Verify gone
        resp = api.get("/admin/groups", headers=admin_headers)
        assert not any(g["id"] == group_id for g in resp.json())

    def test_add_and_list_members(self, api, admin_headers, user_a):
        # Create a group
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "team-a"},
        )
        group_id = resp.json()["id"]

        # Get user A's ID
        resp = api.get("/admin/users", headers=admin_headers)
        users = resp.json()
        alice = next(u for u in users if u["email"] == user_a["email"])

        # Add user A to group
        resp = api.post(
            f"/admin/groups/{group_id}/members",
            headers=admin_headers,
            json={"user_id": alice["id"]},
        )
        assert resp.status_code == 200

        # List members
        resp = api.get(
            f"/admin/groups/{group_id}/members", headers=admin_headers
        )
        assert resp.status_code == 200
        members = resp.json()
        assert len(members) == 1
        assert members[0]["email"] == user_a["email"]

    def test_remove_member(self, api, admin_headers, user_b):
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "temp-group"},
        )
        group_id = resp.json()["id"]

        resp = api.get("/admin/users", headers=admin_headers)
        bob = next(u for u in resp.json() if u["email"] == user_b["email"])

        api.post(
            f"/admin/groups/{group_id}/members",
            headers=admin_headers,
            json={"user_id": bob["id"]},
        )
        resp = api.delete(
            f"/admin/groups/{group_id}/members/{bob['id']}",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        resp = api.get(
            f"/admin/groups/{group_id}/members", headers=admin_headers
        )
        assert resp.json() == []


# --- Permission denials ---


class TestPermissionDenials:
    def test_non_admin_cannot_list_groups(self, api, user_a):
        resp = api.get("/admin/groups", headers=user_a["headers"])
        assert resp.status_code == 403

    def test_non_admin_cannot_create_group(self, api, user_a):
        resp = api.post(
            "/admin/groups",
            headers=user_a["headers"],
            json={"name": "hacker-group"},
        )
        assert resp.status_code == 403

    def test_non_admin_cannot_list_users(self, api, user_a):
        resp = api.get("/admin/users", headers=user_a["headers"])
        assert resp.status_code == 403

    def test_non_admin_cannot_create_user(self, api, user_a):
        resp = api.post(
            "/admin/users",
            headers=user_a["headers"],
            json={"email": "evil@example.com", "password": "testpass"},
        )
        assert resp.status_code == 403

    def test_non_admin_cannot_view_acl_tree(self, api, user_a):
        resp = api.get("/admin/acl/tree", headers=user_a["headers"])
        assert resp.status_code == 403

    def test_non_admin_cannot_delete_other_workspace(
        self, api, user_a, user_b
    ):
        # User B creates a workspace
        resp = api.post(
            "/workspaces",
            headers=user_b["headers"],
            json={"name": _ws_name("bobs-ws")},
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]

        # User A tries to delete it — no ACL entry for A on this workspace
        resp = api.delete(f"/workspaces/{ws_id}", headers=user_a["headers"])
        assert resp.status_code == 403

    def test_non_admin_cannot_update_other_workspace(
        self, api, user_a, user_b
    ):
        # User B creates a workspace
        resp = api.post(
            "/workspaces",
            headers=user_b["headers"],
            json={"name": _ws_name("bobs-ws")},
        )
        ws_id = resp.json()["id"]

        # User A tries to edit it
        resp = api.put(
            f"/workspaces/{ws_id}",
            headers=user_a["headers"],
            json={"name": "hijacked"},
        )
        assert resp.status_code == 403

    def test_unauthenticated_denied(self, api):
        resp = api.get("/workspaces")
        assert resp.status_code == 401

        resp = api.get("/admin/users")
        assert resp.status_code == 401


# --- ACL tree and introspection ---


class TestACLIntrospection:
    def test_acl_tree(self, api, admin_headers):
        resp = api.get("/admin/acl/tree", headers=admin_headers)
        assert resp.status_code == 200
        tree = resp.json()
        resources = [t["resource"] for t in tree]
        assert "/" in resources
        assert "/admin" in resources

    def test_acl_by_group(self, api, admin_headers):
        # Get admin group ID
        resp = api.get("/admin/groups", headers=admin_headers)
        admin_group = next(g for g in resp.json() if g["name"] == "admin")

        resp = api.get(
            f"/admin/acl/by-principal/group/{admin_group['id']}",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) > 0
        # Admin group should have an ACE on /admin
        assert any(e["resource"] == "/admin" for e in entries)

    def test_acl_by_user(self, api, admin_headers, user_a):
        # Create a workspace as user A so they have an ACE
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("introspect")},
        )
        ws_id = resp.json()["id"]

        # Get user A's ID
        resp = api.get("/admin/users", headers=admin_headers)
        alice = next(u for u in resp.json() if u["email"] == user_a["email"])

        resp = api.get(
            f"/admin/acl/by-principal/user/{alice['id']}",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        entries = resp.json()
        # User A should have an ACE on their workspace
        assert any(e["resource"] == f"/workspaces/{ws_id}" for e in entries)

    def test_my_permissions_admin(self, api, admin_headers):
        resp = api.get("/api/my-permissions", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "admin@example.com"
        assert "/admin" in data["permissions"]
        assert "*" in data["permissions"]["/admin"]
        assert len(data["groups"]) > 0
        assert any(g["name"] == "admin" for g in data["groups"])

    def test_my_permissions_regular_user(self, api, user_a):
        resp = api.get("/api/my-permissions", headers=user_a["headers"])
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == user_a["email"]
        # Regular user should NOT have admin permissions
        assert "/admin" not in data["permissions"]
        # But should have view on /
        assert "/" in data["permissions"]
        assert "view" in data["permissions"]["/"]

    def test_my_permissions_for_workspace(self, api, user_a):
        """Check permissions for a specific workspace resource."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("perm")},
        )
        ws_id = resp.json()["id"]
        resp = api.get(
            f"/api/my-permissions?resource=/workspaces/{ws_id}",
            headers=user_a["headers"],
        )
        assert resp.status_code == 200
        data = resp.json()
        perms = data["permissions"].get(f"/workspaces/{ws_id}", [])
        assert "*" in perms

    def test_my_permissions_for_unowned_workspace(self, api, user_a, user_b):
        """User without ACE only gets inherited permissions, not owner perms."""
        resp = api.post(
            "/workspaces",
            headers=user_b["headers"],
            json={"name": _ws_name("other-perm")},
        )
        ws_id = resp.json()["id"]
        resp = api.get(
            f"/api/my-permissions?resource=/workspaces/{ws_id}",
            headers=user_a["headers"],
        )
        assert resp.status_code == 200
        perms = resp.json()["permissions"].get(f"/workspaces/{ws_id}", [])
        # Should NOT have owner-level permissions
        assert "*" not in perms
        assert "terminal" not in perms
        assert "files" not in perms
        assert "share" not in perms

    def test_my_permissions_shared_workspace(self, api, user_a, user_b):
        """Shared user gets view/terminal/files but not share or *."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("shared-perm")},
        )
        ws_id = resp.json()["id"]
        # Share with B
        api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )
        # Check B's permissions
        resp = api.get(
            f"/api/my-permissions?resource=/workspaces/{ws_id}",
            headers=user_b["headers"],
        )
        assert resp.status_code == 200
        perms = resp.json()["permissions"].get(f"/workspaces/{ws_id}", [])
        assert "view" in perms
        assert "terminal" in perms
        assert "files" in perms
        assert "chat" in perms
        assert "*" not in perms
        assert "share" not in perms


# --- Workspace sharing via ACL ---


class TestWorkspaceSharingACL:
    def test_share_workspace_and_access(self, api, user_a, user_b):
        # User A creates a workspace
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("shared")},
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]

        # User B cannot see it in shared list yet
        resp = api.get("/workspaces/shared", headers=user_b["headers"])
        assert not any(w["id"] == ws_id for w in resp.json())

        # User A shares with user B
        resp = api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "shared"

        # User B now sees it in shared list
        resp = api.get("/workspaces/shared", headers=user_b["headers"])
        shared = resp.json()
        assert any(w["id"] == ws_id for w in shared)
        shared_ws = next(w for w in shared if w["id"] == ws_id)
        assert shared_ws["owner_email"] == user_a["email"]

    def test_shared_user_cannot_reshare(self, api, user_a, user_b):
        """User B (shared, not owner) cannot manage members."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("no-reshare")},
        )
        ws_id = resp.json()["id"]

        # Share with B
        api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )

        # B tries to list members — no share permission
        resp = api.get(
            f"/workspaces/{ws_id}/members", headers=user_b["headers"]
        )
        assert resp.status_code == 403

        # B tries to add someone
        resp = api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_b["headers"],
            json={"email": "admin@example.com"},
        )
        assert resp.status_code == 403

    def test_unshare_workspace(self, api, user_a, user_b):
        """Owner can remove a shared user."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("unshare")},
        )
        ws_id = resp.json()["id"]

        # Share with B
        resp = api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )
        member_id = resp.json()["user_id"]

        # Verify B sees it
        resp = api.get("/workspaces/shared", headers=user_b["headers"])
        assert any(w["id"] == ws_id for w in resp.json())

        # Unshare
        resp = api.delete(
            f"/workspaces/{ws_id}/members/{member_id}",
            headers=user_a["headers"],
        )
        assert resp.status_code == 200

        # B no longer sees it
        resp = api.get("/workspaces/shared", headers=user_b["headers"])
        assert not any(w["id"] == ws_id for w in resp.json())

    def test_members_list(self, api, user_a, user_b):
        """Owner can list workspace members."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("members")},
        )
        ws_id = resp.json()["id"]

        # Initially empty (owner is excluded)
        resp = api.get(
            f"/workspaces/{ws_id}/members", headers=user_a["headers"]
        )
        assert resp.status_code == 200
        assert resp.json() == []

        # Share with B
        api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )

        # Now B shows up
        resp = api.get(
            f"/workspaces/{ws_id}/members", headers=user_a["headers"]
        )
        members = resp.json()
        assert len(members) == 1
        assert members[0]["email"] == user_b["email"]

    def test_workspace_delete_cleans_acl(self, api, admin_headers, user_a):
        """Deleting a workspace removes its ACL entries."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("delete-acl")},
        )
        ws_id = resp.json()["id"]

        # ACL tree should include this workspace
        resp = api.get("/admin/acl/tree", headers=admin_headers)
        assert any(
            t["resource"] == f"/workspaces/{ws_id}" for t in resp.json()
        )

        # Delete the workspace
        resp = api.delete(f"/workspaces/{ws_id}", headers=user_a["headers"])
        assert resp.status_code == 200

        # ACL tree should no longer include this workspace
        resp = api.get("/admin/acl/tree", headers=admin_headers)
        assert not any(
            t["resource"] == f"/workspaces/{ws_id}" for t in resp.json()
        )


# --- Cross-cutting: granting admin to a regular user ---


class TestGrantAdminViaGroup:
    def test_add_user_to_admin_group_grants_access(
        self, api, admin_headers, user_a
    ):
        """Adding a user to the admin group gives them admin permissions."""
        # Verify user A cannot access admin endpoints
        resp = api.get("/admin/users", headers=user_a["headers"])
        assert resp.status_code == 403

        # Get admin group and user A's ID
        resp = api.get("/admin/groups", headers=admin_headers)
        admin_group = next(g for g in resp.json() if g["name"] == "admin")

        resp = api.get("/admin/users", headers=admin_headers)
        alice = next(u for u in resp.json() if u["email"] == user_a["email"])

        # Add user A to admin group
        resp = api.post(
            f"/admin/groups/{admin_group['id']}/members",
            headers=admin_headers,
            json={"user_id": alice["id"]},
        )
        assert resp.status_code == 200

        # Now user A needs a fresh token (re-login) — group membership
        # is checked on every request, not cached in JWT
        resp = api.get("/admin/users", headers=user_a["headers"])
        assert resp.status_code == 200
        assert len(resp.json()) > 0

        # Clean up — remove user A from admin group
        resp = api.delete(
            f"/admin/groups/{admin_group['id']}/members/{alice['id']}",
            headers=admin_headers,
        )
        assert resp.status_code == 200

        # Verify access revoked immediately (no re-login needed)
        resp = api.get("/admin/users", headers=user_a["headers"])
        assert resp.status_code == 403


# --- Cascade and edge case tests ---


class TestACLCascades:
    def test_user_delete_cascades_aces(self, api, admin_headers):
        """Deleting a user removes their ACEs from all resources."""
        # Create a dedicated user for this test (not the shared user_a)
        cascade_headers = _register(
            api, f"cascade-{uuid.uuid4().hex[:8]}@example.com"
        )

        # User creates a workspace (gets owner ACE)
        api.post(
            "/workspaces",
            headers=cascade_headers,
            json={"name": _ws_name("cascade")},
        )

        # Get user's ID
        resp = api.get("/admin/users", headers=admin_headers)
        target = next(
            u for u in resp.json() if u["email"].startswith("cascade-")
        )

        # Verify ACE exists
        resp = api.get(
            f"/admin/acl/by-principal/user/{target['id']}",
            headers=admin_headers,
        )
        assert len(resp.json()) > 0

        # Delete user
        resp = api.delete(
            f"/admin/users/{target['id']}", headers=admin_headers
        )
        assert resp.status_code == 200

        # ACEs should be gone
        resp = api.get(
            f"/admin/acl/by-principal/user/{target['id']}",
            headers=admin_headers,
        )
        assert resp.json() == []

    def test_group_delete_cascades_aces(self, api, admin_headers):
        """Deleting a group removes its ACEs from all resources."""
        # Create a group with an ACE
        resp = api.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "cascade-group"},
        )
        group_id = resp.json()["id"]

        # Verify group shows in ACL queries
        resp = api.get(
            f"/admin/acl/by-principal/group/{group_id}",
            headers=admin_headers,
        )
        # No ACEs yet for this group — that's fine, CASCADE is on FK

        # Delete the group
        resp = api.delete(f"/admin/groups/{group_id}", headers=admin_headers)
        assert resp.status_code == 200

        # Group should be gone
        resp = api.get("/admin/groups", headers=admin_headers)
        assert not any(g["id"] == group_id for g in resp.json())


class TestSharedWorkspaceAccess:
    def test_shared_user_cannot_delete_workspace(self, api, user_a, user_b):
        """Shared user gets 403 when trying to delete the workspace."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("no-delete")},
        )
        ws_id = resp.json()["id"]

        # Share with B
        api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )

        # B tries to delete — should be denied
        resp = api.delete(f"/workspaces/{ws_id}", headers=user_b["headers"])
        assert resp.status_code == 403

    def test_shared_user_cannot_edit_workspace(self, api, user_a, user_b):
        """Shared user gets 403 when trying to edit the workspace."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("no-edit")},
        )
        ws_id = resp.json()["id"]

        api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )

        resp = api.put(
            f"/workspaces/{ws_id}",
            headers=user_b["headers"],
            json={"name": "hijacked"},
        )
        assert resp.status_code == 403

    def test_unshare_revokes_access_immediately(self, api, user_a, user_b):
        """Removing a shared user immediately denies their access."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("revoke")},
        )
        ws_id = resp.json()["id"]

        # Share with B
        resp = api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_b["email"]},
        )
        member_id = resp.json()["user_id"]

        # B can see permissions
        resp = api.get(
            f"/api/my-permissions?resource=/workspaces/{ws_id}",
            headers=user_b["headers"],
        )
        perms = resp.json()["permissions"].get(f"/workspaces/{ws_id}", [])
        assert "view" in perms

        # Unshare
        api.delete(
            f"/workspaces/{ws_id}/members/{member_id}",
            headers=user_a["headers"],
        )

        # B immediately loses workspace-specific permissions
        # (view and create are inherited from / and /workspaces)
        resp = api.get(
            f"/api/my-permissions?resource=/workspaces/{ws_id}",
            headers=user_b["headers"],
        )
        perms = resp.json()["permissions"].get(f"/workspaces/{ws_id}", [])
        assert "terminal" not in perms
        assert "files" not in perms
        assert "chat" not in perms
        assert "*" not in perms

    def test_add_self_as_member_rejected(self, api, user_a):
        """Owner cannot share a workspace with themselves."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("self-share")},
        )
        ws_id = resp.json()["id"]

        resp = api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": user_a["email"]},
        )
        assert resp.status_code == 400
        assert "yourself" in resp.json()["detail"]

    def test_add_nonexistent_user_as_member(self, api, user_a):
        """Sharing with a nonexistent email returns 404."""
        resp = api.post(
            "/workspaces",
            headers=user_a["headers"],
            json={"name": _ws_name("nouser")},
        )
        ws_id = resp.json()["id"]

        resp = api.post(
            f"/workspaces/{ws_id}/members",
            headers=user_a["headers"],
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 404
        assert "User not found" in resp.json()["detail"]


class TestAdminResourceACL:
    def test_get_workspaces_acl(self, api, admin_headers):
        """Admin can read the /workspaces static resource ACL."""
        resp = api.get(
            "/admin/acl/resource?resource=/workspaces",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        entries = resp.json()
        assert any(e["permission"] == "create" for e in entries)
        assert any(e["principal"] == "Authenticated" for e in entries)

    def test_modify_workspaces_acl(self, api, admin_headers):
        """Admin can add and remove ACEs on /workspaces."""
        # Get current
        resp = api.get(
            "/admin/acl/resource?resource=/workspaces",
            headers=admin_headers,
        )
        original = resp.json()

        # Add a view ACE
        new_entries = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in original
        ] + [
            {
                "action": 1,
                "principal_type": 0,
                "permission": "view",
                "user_id": None,
                "group_id": None,
                "system_principal": 1,
            },
        ]
        resp = api.put(
            "/admin/acl/resource?resource=/workspaces",
            headers=admin_headers,
            json=new_entries,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == len(original) + 1

        # Restore original
        restore = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in original
        ]
        resp = api.put(
            "/admin/acl/resource?resource=/workspaces",
            headers=admin_headers,
            json=restore,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == len(original)

    def test_non_admin_denied(self, api, user_a):
        """Non-admin cannot access the resource ACL endpoint."""
        resp = api.get(
            "/admin/acl/resource?resource=/workspaces",
            headers=user_a["headers"],
        )
        assert resp.status_code == 403
