"""Per-user HOME directory E2E tests.

Tests that handles are auto-created on first connection, HOME is set
correctly in terminal/exec sessions, and handle changes work.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e
"""

import asyncio
import json

import httpx
import pytest

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module."""
    server = start_server(
        KLANGKD_JWT_SECRET="home-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        KLANGKD_LLM_BASE_URL="",
        KLANGKD_LLM_API_KEY="",
        KLANGKD_LLM_MODEL="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def auth(server):
    """Login and return token + headers."""
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return {"token": token, "headers": headers}


_ws_counter = 0


def create_workspace(server, auth):
    global _ws_counter  # noqa: PLW0603
    _ws_counter += 1
    name = f"home-e2e-{_ws_counter}"
    client = server["client"]
    resp = client.post(
        "/api/v1/workspaces",
        headers=auth["headers"],
        json={"name": name},
        timeout=10,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    def cleanup():
        try:
            client.delete(
                f"/api/v1/workspaces/{workspace_id}",
                headers=auth["headers"],
                timeout=30,
            )
        except httpx.ReadTimeout:
            pass

    return workspace_id, cleanup


async def ws_connect(server, auth, workspace_id):
    """Open a WebSocket, connect to workspace, wait for container ready."""
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    # Drain until container_ready
    deadline = asyncio.get_event_loop().time() + 60
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if msg.get("type") == "container_ready":
            break
    await ws.send(json.dumps({"cmd": "ui_ready"}))
    # Wait for container_ready (handle is auto-set during ui_ready)
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if is_container_ready(msg):
            break
    return ws


async def recv_until(ws, predicate, timeout=30):
    deadline = asyncio.get_event_loop().time() + timeout
    messages = []
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=1)
            data = json.loads(msg)
            messages.append(data)
            if predicate(data):
                return messages
        except asyncio.TimeoutError:
            continue
    return messages


def is_container_ready(msg):
    if msg.get("type") != "event":
        return False
    event = msg.get("event", {})
    return (
        event.get("type") == "CUSTOM"
        and event.get("name") == "container_ready"
    )


def is_exec_exit(msg):
    return msg.get("type") == "exec_exit"


async def exec_command(ws, command):
    """Run a command via exec and return combined output (base64-decoded)."""
    import base64

    await ws.send(json.dumps({"cmd": "exec_start", "command": command}))
    msgs = await recv_until(ws, is_exec_exit, timeout=15)
    outputs = [m for m in msgs if m.get("type") == "exec_output"]
    return b"".join(base64.b64decode(m["data"]) for m in outputs).decode()


class TestAutoHandle:
    @pytest.mark.asyncio
    async def test_handle_auto_created_on_first_connect(self, server, auth):
        """First connection auto-creates a handle from the email.

        ws_connect waits for container_ready, which only happens after
        the handle is auto-created. If we get here, it worked.
        """
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                # Verify the handle exists by checking $HOME
                output = await exec_command(ws, ["bash", "-c", "echo $HOME"])
                assert "/home/test" in output
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_handle_reused_on_reconnect(self, server, auth):
        """Second connection reuses the existing handle."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            # First connection — auto-creates handle
            ws1 = await ws_connect(server, auth, workspace_id)
            output1 = await exec_command(ws1, ["bash", "-c", "echo $HOME"])
            await ws1.close()

            # Second connection — reuses handle
            ws2 = await ws_connect(server, auth, workspace_id)
            try:
                output2 = await exec_command(ws2, ["bash", "-c", "echo $HOME"])
                assert output1.strip() == output2.strip()
            finally:
                await ws2.close()
        finally:
            cleanup()


class TestPerUserHome:
    @pytest.mark.asyncio
    async def test_home_set_to_handle_dir(self, server, auth):
        """Terminal exec session gets HOME=/home/<handle>."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                output = await exec_command(ws, ["bash", "-c", "echo $HOME"])
                # Handle is derived from "test@example.com" → "test"
                assert "/home/test" in output
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_handle_is_symlink(self, server, auth):
        """The handle dir is a symlink to .users/<uuid>."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                output = await exec_command(
                    ws, ["bash", "-c", "readlink /home/test"]
                )
                assert ".users/" in output
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_shared_work_dir_accessible(self, server, auth):
        """Shared /home/work directory is accessible and writable."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                output = await exec_command(
                    ws,
                    [
                        "bash",
                        "-c",
                        "touch /home/work/.e2e-test && echo ok",
                    ],
                )
                assert "ok" in output
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_user_home_is_writable(self, server, auth):
        """Files written to $HOME persist in the per-user directory."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                output = await exec_command(
                    ws,
                    [
                        "bash",
                        "-c",
                        "echo hello > $HOME/.e2e-file && cat $HOME/.e2e-file",
                    ],
                )
                assert "hello" in output
            finally:
                await ws.close()
        finally:
            cleanup()


class TestBashrc:
    @pytest.mark.asyncio
    async def test_user_bashrc_sourced_in_login_shell(self, server, auth):
        """~/.bashrc is sourced when a login shell starts (as tmux does).

        tmux starts bash as a login shell for new windows.  Login bash
        reads /etc/profile then ~/.profile — but per-user HOME dirs
        don't get /etc/skel/.profile, so ~/.bashrc is never sourced
        unless /etc/bash.bashrc or ~/.profile does it explicitly.
        """
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                # Write a .bashrc that sets a marker env var
                await exec_command(
                    ws,
                    [
                        "bash",
                        "-c",
                        'echo "export BASHRC_MARKER=hello_from_bashrc" '
                        "> $HOME/.bashrc",
                    ],
                )
                # Start a login shell (same as tmux new-window does)
                output = await exec_command(
                    ws,
                    ["bash", "-lic", "echo $BASHRC_MARKER"],
                )
                assert "hello_from_bashrc" in output
            finally:
                await ws.close()
        finally:
            cleanup()


class TestFileApiNavigation:
    @pytest.mark.asyncio
    async def test_list_home_shows_user_homedir_symlink_as_directory(
        self, server, auth
    ):
        """Listing /home via the file API shows the user's homedir symlink
        as a directory (not a file), so clicking it navigates correctly."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                # Get the user's handle from $HOME
                output = await exec_command(
                    ws, ["bash", "-c", "basename $HOME"]
                )
                handle = output.strip()

                # List /home via the file API
                client = server["client"]
                resp = client.get(
                    f"/api/v1/workspaces/{workspace_id}/files",
                    params={"path": "/home"},
                    headers=auth["headers"],
                    timeout=10,
                )
                assert resp.status_code == 200
                entries = resp.json()
                homedir_entry = [e for e in entries if e["name"] == handle]
                assert len(homedir_entry) == 1, (
                    f"Expected homedir entry '{handle}' in /home listing, "
                    f"got: {[e['name'] for e in entries]}"
                )
                assert homedir_entry[0]["is_dir"] is True, (
                    f"Homedir symlink '{handle}' should appear as a "
                    f"directory, got is_dir={homedir_entry[0]['is_dir']}"
                )
                assert homedir_entry[0]["path"] == f"/home/{handle}"
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_list_root_includes_home(self, server, auth):
        """Listing / via the file API includes the /home directory."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                client = server["client"]
                resp = client.get(
                    f"/api/v1/workspaces/{workspace_id}/files",
                    params={"path": "/"},
                    headers=auth["headers"],
                    timeout=10,
                )
                assert resp.status_code == 200
                entries = resp.json()
                names = [e["name"] for e in entries]
                assert "home" in names
                home_entry = [e for e in entries if e["name"] == "home"][0]
                assert home_entry["is_dir"] is True
                assert home_entry["path"] == "/home"
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_download_homedir_symlink_as_tar(self, server, auth):
        """Downloading the user's homedir (a symlink to a directory)
        produces a non-empty .tar.gz archive."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                # Create a file so the archive has content
                await exec_command(
                    ws, ["bash", "-c", "echo hello > ~/testfile.txt"]
                )

                # Get the handle
                output = await exec_command(
                    ws, ["bash", "-c", "basename $HOME"]
                )
                handle = output.strip()

                client = server["client"]
                # Verify the path exists and is a directory
                list_resp = client.get(
                    f"/api/v1/workspaces/{workspace_id}/files",
                    params={"path": f"/home/{handle}"},
                    headers=auth["headers"],
                    timeout=10,
                )
                assert list_resp.status_code == 200, (
                    f"Listing /home/{handle} failed: {list_resp.status_code}"
                )
                entries = list_resp.json()
                assert len(entries) > 0, f"/home/{handle} listing is empty"

                # First verify /tmp download works (not a symlink)
                await exec_command(
                    ws, ["bash", "-c", "echo tmptest > /tmp/check.txt"]
                )
                tmp_resp = client.get(
                    f"/api/v1/workspaces/{workspace_id}/files/download",
                    params={"path": "/tmp"},
                    headers=auth["headers"],
                    timeout=30,
                )
                assert tmp_resp.status_code == 200
                assert len(tmp_resp.content) > 0, (
                    "/tmp download is empty (streaming broken)"
                )

                # Now test the symlinked homedir
                resp = client.get(
                    f"/api/v1/workspaces/{workspace_id}/files/download",
                    params={"path": f"/home/{handle}"},
                    headers=auth["headers"],
                    timeout=30,
                )
                assert resp.status_code == 200, (
                    f"Download failed: {resp.status_code} {resp.text}"
                )
                assert resp.headers["content-type"] == "application/gzip", (
                    f"Expected tar path but got content-type: "
                    f"{resp.headers['content-type']}"
                )
                assert len(resp.content) > 0, (
                    f"Downloaded archive for /home/{handle} is empty"
                )
                assert resp.headers["content-type"] == "application/gzip"
            finally:
                await ws.close()
        finally:
            cleanup()


class TestHandleChange:
    @pytest.mark.asyncio
    async def test_change_handle_via_set_handle(self, server, auth):
        """User can change their handle via set_handle command."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                # Change handle
                await ws.send(
                    json.dumps({"cmd": "set_handle", "handle": "newname"})
                )
                msgs = await recv_until(
                    ws, lambda m: m.get("type") == "handle_set", timeout=10
                )
                handle_set = [m for m in msgs if m.get("type") == "handle_set"]
                assert len(handle_set) == 1
                assert handle_set[0]["handle"] == "newname"
                assert handle_set[0]["home"] == "/home/newname"
            finally:
                await ws.close()
        finally:
            cleanup()
