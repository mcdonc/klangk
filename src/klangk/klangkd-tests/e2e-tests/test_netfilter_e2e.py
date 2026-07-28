"""End-to-end tests for per-workspace egress filtering (#1365, #1959).

Verifies that the netfilter OCI hook fires at container creation time and
installs iptables rules in the container's network namespace.  On macOS
(podman machine), the hooks are installed inside the VM by
``_install_hooks_in_vm()``; on Linux, they are written to the host
filesystem.  Either way, the observable behavior is the same: a filtered
container has an OUTPUT DROP policy + per-destination ACCEPT rules.

The iptables check runs via ``nsenter`` from the host (Linux) or from
inside the VM (macOS via ``podman machine ssh``) because the workspace
container image may not have ``iptables`` installed.

These tests do NOT verify *enforcement* (actually blocking traffic) —
that requires real DNS and remote hosts.  They verify the *mechanism*:
the hook fires, iptables rules are present, and the OUTPUT policy is
DROP.  ``scripts/test-netfilter.sh`` is the manual enforcement test.

Requires: podman available (rootless on Linux, podman machine on macOS),
klangk workspace image built.

Run with: devenv shell -- test-backend-e2e test_netfilter_e2e.py
"""

import platform
import subprocess
import time

import pytest

from _e2e_server import start_server, stop_server


@pytest.fixture(scope="module")
def server():
    """Start a real klangkd server with netfilter enabled (the default)."""
    server = start_server(
        KLANGKD_JWT_SECRET="netfilter-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_ALLOW_AUTOSTART="1",
        # netfilter_enabled defaults True; no need to set it explicitly.
        LOGFIRE_TOKEN="",
        KLANGKD_LLM_BASE_URL="",
        KLANGKD_LLM_API_KEY="",
        KLANGKD_LLM_MODEL="",
    )
    config_resp = server["client"].get("/api/v1/config", timeout=10)
    server["instance_id"] = (
        config_resp.json().get("instance_id", "")
        if config_resp.status_code == 200
        else ""
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def auth(server):
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


_ws_counter = 0


def _create_workspace(server, auth, *, allowed_domains=None):
    """Create an auto-started workspace; return (workspace_id, cleanup)."""
    global _ws_counter  # noqa: PLW0603
    _ws_counter += 1
    name = f"nf-e2e-{_ws_counter}"
    body = {
        "name": name,
        "auto_start": True,
        "setup_state": "complete",
    }
    if allowed_domains is not None:
        body["allowed_domains"] = allowed_domains
    client = server["client"]
    resp = client.post(
        "/api/v1/workspaces",
        headers=auth,
        json=body,
        timeout=30,
    )
    assert resp.status_code == 200, f"create workspace failed: {resp.text}"
    workspace_id = resp.json()["id"]

    def cleanup():
        try:
            client.post(
                f"/api/v1/workspaces/{workspace_id}/stop",
                headers=auth,
                timeout=30,
            )
        except Exception:
            pass
        try:
            client.delete(
                f"/api/v1/workspaces/{workspace_id}",
                headers=auth,
                timeout=30,
            )
        except Exception:
            pass

    return workspace_id, cleanup


def _wait_for_container(workspace_id, instance_id, timeout=60):
    """Wait for the workspace container to appear; return its id."""
    name = f"klangk-{instance_id}-{workspace_id[:12]}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["podman", "ps", "--filter", f"name=^{name}$", "-q"],
            capture_output=True,
            text=True,
        )
        cids = [c for c in result.stdout.strip().split() if c]
        if cids:
            return cids[0]
        time.sleep(0.5)
    raise AssertionError(
        f"container for workspace {workspace_id} did not appear "
        f"within {timeout}s"
    )


def _get_iptables(cid, *, v6=False, verbose=False):
    """Get iptables OUTPUT chain listing for a container.

    Runs nsenter from the host (Linux) or via podman machine ssh (macOS)
    to check iptables in the container's network namespace.  The workspace
    image may not have iptables installed, so we never exec inside the
    container itself.

    Returns (ok, output) where ok=False means iptables is unavailable.
    """
    cmd_name = "ip6tables" if v6 else "iptables"
    pid_result = subprocess.run(
        ["podman", "inspect", cid, "--format", "{{.State.Pid}}"],
        capture_output=True,
        text=True,
    )
    pid = pid_result.stdout.strip()
    if not pid or pid == "0":
        return False, "container PID not available"

    v_flag = " -v" if verbose else ""
    nsenter_cmd = (
        f"nsenter --net=/proc/{pid}/ns/net {cmd_name} -L OUTPUT -n{v_flag}"
    )
    if platform.system() == "Darwin":
        cmd = ["podman", "machine", "ssh", f"sudo {nsenter_cmd}"]
    else:
        parts = [
            "sudo",
            "nsenter",
            f"--net=/proc/{pid}/ns/net",
            cmd_name,
            "-L",
            "OUTPUT",
            "-n",
        ]
        if verbose:
            parts.append("-v")
        cmd = parts

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return False, result.stderr
    return True, result.stdout


class TestNetfilterE2E:
    """Verify the netfilter OCI hook fires and installs iptables rules.

    Uses auto_start=True and checks iptables from outside the container
    via nsenter (not from inside, since the workspace image may lack
    iptables).
    """

    def test_filtered_container_has_drop_policy(self, server, auth):
        """A workspace with allowed_domains gets an OUTPUT DROP policy.

        This is the fundamental netfilter contract: the hook fires at
        container creation, sets OUTPUT policy to DROP, and adds ACCEPT
        rules for the allowed destinations.  If this fails, the hook
        didn't fire (the container started unrestricted).
        """
        workspace_id, cleanup = _create_workspace(
            server, auth, allowed_domains=["github.com:443"]
        )
        try:
            cid = _wait_for_container(workspace_id, server["instance_id"])
            ok, output = _get_iptables(cid)
            if not ok:
                pytest.skip(f"iptables not available: {output}")
            first_line = output.strip().split("\n")[0]
            assert "DROP" in first_line, (
                f"OUTPUT policy is not DROP — the netfilter hook did not "
                f"fire.\niptables output:\n{output}"
            )
        finally:
            cleanup()

    def test_filtered_container_has_accept_rules(self, server, auth):
        """A filtered container has ACCEPT rules for allowed destinations.

        The hook resolves allowed_domains to IPs and adds per-IP ACCEPT
        rules.  We check that at least one ACCEPT rule with dpt:443
        exists (from the github.com:443 spec).
        """
        workspace_id, cleanup = _create_workspace(
            server, auth, allowed_domains=["github.com:443"]
        )
        try:
            cid = _wait_for_container(workspace_id, server["instance_id"])
            ok, output = _get_iptables(cid)
            if not ok:
                pytest.skip(f"iptables not available: {output}")
            lines = output.strip().split("\n")
            accept_443 = [
                ln for ln in lines if "ACCEPT" in ln and "dpt:443" in ln
            ]
            assert accept_443, (
                f"No ACCEPT rule for dpt:443 found — the hook didn't "
                f"install per-destination rules.\n"
                f"iptables output:\n{output}"
            )
        finally:
            cleanup()

    def test_filtered_container_allows_loopback(self, server, auth):
        """Loopback traffic is always allowed in a filtered container.

        The hook's ``-A OUTPUT -o lo -j ACCEPT`` rule shows up in
        ``iptables -L -n -v`` with interface ``lo``, but in the compact
        ``-L -n`` output it appears as an ACCEPT with 0.0.0.0/0 → 0.0.0.0/0
        and no further match criteria (the first rule after the policy).
        Use ``-L -n -v`` to see the interface column.
        """
        workspace_id, cleanup = _create_workspace(
            server, auth, allowed_domains=["github.com:443"]
        )
        try:
            cid = _wait_for_container(workspace_id, server["instance_id"])
            ok, output = _get_iptables(cid, verbose=True)
            if not ok:
                pytest.skip(f"iptables not available: {output}")
            assert any(
                "ACCEPT" in ln and "lo" in ln
                for ln in output.strip().split("\n")
            ), f"No loopback ACCEPT rule found.\niptables output:\n{output}"
        finally:
            cleanup()

    def test_unfiltered_container_has_accept_policy(self, server, auth):
        """A workspace without allowed_domains has the default ACCEPT policy.

        The hook should NOT fire for an unfiltered workspace (the hook
        JSON's annotation filter gates on the annotation's presence).
        """
        workspace_id, cleanup = _create_workspace(server, auth)
        try:
            cid = _wait_for_container(workspace_id, server["instance_id"])
            ok, output = _get_iptables(cid)
            if not ok:
                pytest.skip(f"iptables not available: {output}")
            first_line = output.strip().split("\n")[0]
            assert "ACCEPT" in first_line, (
                f"Unfiltered workspace should have OUTPUT ACCEPT policy, "
                f"got: {first_line}"
            )
        finally:
            cleanup()

    def test_filtered_container_drops_ipv6(self, server, auth):
        """IPv6 OUTPUT policy is DROP in a filtered container (#1936)."""
        workspace_id, cleanup = _create_workspace(
            server, auth, allowed_domains=["github.com:443"]
        )
        try:
            cid = _wait_for_container(workspace_id, server["instance_id"])
            ok, output = _get_iptables(cid, v6=True)
            if not ok:
                pytest.skip(f"ip6tables not available: {output}")
            first_line = output.strip().split("\n")[0]
            assert "DROP" in first_line, (
                f"IPv6 OUTPUT policy should be DROP, got: {first_line}"
            )
        finally:
            cleanup()

    def test_netfilter_with_cidr_spec(self, server, auth):
        """A CIDR spec (10.0.0.0/8) is installed as an iptables range rule."""
        workspace_id, cleanup = _create_workspace(
            server, auth, allowed_domains=["10.0.0.0/8"]
        )
        try:
            cid = _wait_for_container(workspace_id, server["instance_id"])
            ok, output = _get_iptables(cid)
            if not ok:
                pytest.skip(f"iptables not available: {output}")
            assert any(
                "ACCEPT" in ln and "10.0.0.0/8" in ln
                for ln in output.strip().split("\n")
            ), (
                f"No ACCEPT rule for CIDR 10.0.0.0/8 found.\n"
                f"iptables output:\n{output}"
            )
        finally:
            cleanup()
