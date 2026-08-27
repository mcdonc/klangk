"""E2E: revoking ``exec-and-sync`` blocks the exec channel (#2706).

Proves the security property end-to-end against a real klangkd + real
container: revoking ``exec-and-sync`` BEFORE the tool runs blocks every
consumer of the exec channel for a regular (non-admin) member:

1. admin creates a workspace and shares it with a second user as coder;
2. the member's ``klangk exec`` works (coders seed ``exec-and-sync``);
3. the ``exec-and-sync`` ACE is removed from the member's role group;
4. ``klangk exec`` is rejected with the sync-permission error;
5. ``klangk sync`` is rejected by the CLI preflight before rsync runs;
6. ``klangk sandbox --force`` (re-apply + setup over the exec channel)
   fails: the exec nack surfaces and setup_state flips to ``failed``;
7. restoring the ACE makes ``klangk exec`` work again (the failure was
   the permission, nothing else).
"""

import os
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "src",
        "klangk",
        "klangkd-tests",
        "e2e-tests",
    ),
)
from _e2e_server import start_server, stop_server

ADMIN_EMAIL = "test@example.com"
ADMIN_PASSWORD = "testpass"
MEMBER_EMAIL = "member@example.com"
MEMBER_PASSWORD = "memberpass"
WS = "e2e-revoke-sync"


def _run(args, timeout=300, env=None, input=None):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
        env=env,
    )


class TestRevokeSyncBlocksExec:
    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def server(tmp_path_factory, request):
        data_dir = tempfile.mkdtemp(prefix="klangk-revoke-sync-e2e-")
        log_path = os.path.join(data_dir, "server.log")
        overrides = {
            "KLANGKD_JWT_SECRET": "revoke-sync-e2e-test-secret",
            "KLANGKD_PREVENT_INSECURE_JWT_SECRET": "",
            "KLANGKD_DEFAULT_USER": ADMIN_EMAIL,
            "KLANGKD_DEFAULT_PASSWORD": ADMIN_PASSWORD,
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_TEST_MODE": "1",
            "KLANGKD_IDLE_TIMEOUT_SECONDS": "300",
            "LOGFIRE_TOKEN": "",
            "log_path": log_path,
        }
        server = start_server(uds=False, data_dir=data_dir, **overrides)

        def _login_env(email, password, home):
            env = {**os.environ, "HOME": str(home)}
            os.makedirs(home / ".config" / "klangk", exist_ok=True)
            r = _run(
                ["klangk", "login", server["url"], email, "--password-file", "-"],
                input=password + "\n",
                env=env,
            )
            assert r.returncode == 0, r.stderr
            return env

        admin_home = tmp_path_factory.mktemp("klangk-admin-home")
        member_home = tmp_path_factory.mktemp("klangk-member-home")
        admin_env = _login_env(ADMIN_EMAIL, ADMIN_PASSWORD, admin_home)
        r = httpx.post(
            f"{server['url']}/api/v1/auth/login",
            json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        admin_token = r.json()["access_token"]
        request.cls._admin_headers = {"Authorization": f"Bearer {admin_token}"}
        # Non-admin member (the site admin's '*' on '/' inherits sync
        # everywhere, so the member is the population the gate targets).
        r = httpx.post(
            f"{server['url']}/api/v1/admin/users",
            headers=request.cls._admin_headers,
            json={"email": MEMBER_EMAIL, "password": MEMBER_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        request.cls._member_env = _login_env(MEMBER_EMAIL, MEMBER_PASSWORD, member_home)
        request.cls._base_url = server["url"]
        request.cls._log_path = log_path
        request.cls._admin_env = admin_env
        yield
        stop_server(server)

    def _list_workspaces(self):
        r = httpx.get(
            f"{self._base_url}/api/v1/workspaces",
            headers=self._admin_headers,
            timeout=30,
        )
        r.raise_for_status()
        items = r.json()
        if isinstance(items, dict):
            items = items.get("workspaces") or []
        return items

    def _ws_id(self):
        for ws in self._list_workspaces():
            if ws["name"] == WS:
                return ws["id"]
        raise LookupError(WS)

    def _get_acl(self, ws_id):
        r = httpx.get(
            f"{self._base_url}/api/v1/workspaces/{ws_id}/acl",
            headers=self._admin_headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _put_acl(self, ws_id, entries):
        r = httpx.put(
            f"{self._base_url}/api/v1/workspaces/{ws_id}/acl",
            headers=self._admin_headers,
            json=entries,
            timeout=30,
        )
        r.raise_for_status()

    def _member_id(self):
        r = httpx.get(
            f"{self._base_url}/api/v1/admin/users",
            headers=self._admin_headers,
            timeout=30,
        )
        r.raise_for_status()
        users = r.json()
        if isinstance(users, dict):
            users = users.get("users") or []
        for u in users:
            if u.get("email") == MEMBER_EMAIL:
                return u["id"]
        raise LookupError(MEMBER_EMAIL)

    def _acl_sans_sync(self, acl):
        """The same ACL with every 'exec-and-sync' Allow ACE dropped, plus an
        ``edit`` Allow for the member (sandbox --force's preamble needs
        edit + terminal; coders hold terminal but not edit) — so the
        ONLY effective permission removed is ``exec-and-sync``."""
        member_id = self._member_id()
        rebuilt = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in acl
            if e["permission"] != "exec-and-sync"
        ]
        assert len(rebuilt) < len(acl), "no exec-and-sync ACE found to revoke"
        rebuilt.append(
            {
                "action": 1,
                "principal_type": 1,
                "permission": "edit",
                "user_id": member_id,
                "group_id": None,
                "system_principal": None,
            }
        )
        return rebuilt

    def _plain_acl(self, acl):
        rebuilt = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in acl
        ]
        return rebuilt

    def _setup_state(self, ws_id):
        for ws in self._list_workspaces():
            if ws["id"] == ws_id:
                return ws.get("setup_state")
        raise LookupError(ws_id)

    def _container_running(self, ws_id):
        """(running, container_id) from the status endpoint."""
        r = httpx.get(
            f"{self._base_url}/api/v1/workspaces/{ws_id}/status",
            headers=self._admin_headers,
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        return bool(body.get("running")), body.get("container_id")

    def test_revoking_sync_blocks_exec_sync_and_sandbox(self, tmp_path):
        # 1. Admin creates the workspace, shares it with the member as
        #    coder (coders seed sync), and boots the container.
        r = _run(["klangk", "create", WS], env=self._admin_env)
        assert r.returncode == 0, r.stdout + r.stderr
        r = _run(
            ["klangk", "share", WS, MEMBER_EMAIL, "--role=coder"],
            env=self._admin_env,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        ws_id = self._ws_id()

        # 2. The member can exec while their role group holds sync.
        r = _run(["klangk", "exec", WS, "true"], env=self._member_env)
        assert r.returncode == 0, (
            "member exec should work before revocation:\n" + r.stdout + r.stderr
        )

        original_acl = self._get_acl(ws_id)

        # 3. Revoke: drop the member's role group's sync ACE.
        self._put_acl(ws_id, self._acl_sans_sync(original_acl))

        # 4. klangk exec is rejected by the server-side gate. The CLI's
        #    connect handshake waits for ``container_ready`` — which the
        #    server sends only after the full create-choke-point bring-up
        #    (entrypoint one-time setup, sudo config, workspace token,
        #    FIPS gate) — so the very fact the denial (not a hang or a
        #    silent no-op) arrived proves the creation hook had fired
        #    before the exec was attempted; ExecController.start checks
        #    container_id before the permission. Assert it directly too.
        running, container_id = self._container_running(ws_id)
        assert running and container_id, "precondition: container up"
        r = _run(["klangk", "exec", WS, "echo", "hi"], env=self._member_env)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "exec-and-sync permission" in " ".join((r.stdout + r.stderr).split()), (
            r.stdout + r.stderr
        )
        # The denial did not tear the (creation-hook-provided) container
        # down — bring-up completed and only the member's exec was
        # refused.
        running, container_id_after = self._container_running(ws_id)
        assert running and container_id_after == container_id, (
            "container should still be the same, running instance after the denied exec"
        )

        # 5. klangk sync is rejected by the CLI preflight before rsync.
        dest = tmp_path / "pull-dest"
        dest.mkdir()
        r = _run(
            ["klangk", "sync", f"{WS}:/home/klangk", str(dest)],
            env=self._member_env,
        )
        assert r.returncode == 1, r.stdout + r.stderr
        assert "requires the exec-and-sync permission" in " ".join(r.stderr.split()), (
            r.stdout + r.stderr
        )

        # 6. klangk sandbox --force re-runs setup over the exec channel:
        #    denied, and setup_state flips to 'failed'. (The member needs
        #    edit + terminal for the re-apply preamble -- those stay
        #    granted; only sync was revoked.)
        root = tmp_path / "sbroot"
        root.mkdir()
        (root / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n  mount-at: /sbtest\n  setup: setup.sh\n"
        )
        (root / "setup.sh").write_text("#!/bin/sh\nexit 0\n")
        r = _run(
            ["klangk", "sandbox", WS, str(root), "--force"],
            env=self._member_env,
        )
        combined = " ".join((r.stdout + r.stderr).split())
        assert "exec-and-sync permission" in combined, combined
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._setup_state(ws_id) == "failed":
                break
            time.sleep(2)
        assert self._setup_state(ws_id) == "failed", (
            f"setup_state={self._setup_state(ws_id)!r}, expected 'failed'"
        )
        # The sandbox's --force preamble restarted the container — the
        # creation hook re-fired on that restart's bring-up — and the
        # member's setup exec then hit the sync gate. The nack message
        # itself proves the ordering: the CLI waits for ``container_ready``
        # (sent only after bring-up) before sending ``exec_start``, and
        # ``ExecController.start`` only reaches the permission check with
        # a container_id — a pre-hook exec would hang or no-op, not be
        # denied. The container being stopped afterwards is the sandbox
        # CLI's normal post-setup behavior (#2404), not a hook failure.

        # 7. Restore the ACL; the member's exec works again (causality).
        self._put_acl(ws_id, self._plain_acl(original_acl))
        r = _run(["klangk", "exec", WS, "true"], env=self._member_env)
        assert r.returncode == 0, r.stdout + r.stderr
