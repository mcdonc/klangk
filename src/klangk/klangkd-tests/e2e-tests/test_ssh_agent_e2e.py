"""E2E: SSH agent forwarding into a workspace container (#2001).

The bash/expect ``tests/test_ssh_agent_e2e.sh`` drives the real CLI's TUI
and therefore needs an interactive agent + terminal — it skips in CI. This
suite is the CI-runnable counterpart: it brings its own ``ssh-agent`` +
throwaway key (so no pre-existing agent is required), speaks the WebSocket
agent-relay protocol that ``klangk shell -A`` speaks, and asserts the key is
reachable from inside the container.

It covers the two failure modes #2001 fixed:

1. *No ``SSH_AUTH_SOCK`` in shells created before the relay.* The base
   session must wire the deterministic per-user socket path at creation
   time, so the var is present (inert → live) regardless of when the relay
   starts.
2. *Relay leak on reconnect.* Reconnecting a forwarded agent must not leave
   a competing ``socat`` orphaned on the same ``unlink-early`` socket, which
   would make the path flicker and ``ssh-add`` fail with "No such file or
   directory".

These run under the standard backend-e2e job (``test-backend-e2e``) against
real podman containers, like the rest of this directory.
"""

import asyncio
import base64
import json
import os
import re
import socket
import struct
import subprocess
import time

import pytest
import websockets

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial

_AGENT_PROTOCOL_TIMEOUT = 3.0  # per local-agent round-trip, seconds


def _socat_count(out: str) -> int | None:
    """Parse ``pgrep -c socat`` output: last non-empty integer line."""
    counts = [
        int(ln.strip())
        for ln in out.strip().splitlines()
        if ln.strip().isdigit()
    ]
    return counts[-1] if counts else None


def _have(*bins: str) -> bool:
    return all(shutil_which(b) is not None for b in bins)


def shutil_which(bin_name: str) -> str | None:
    # stdlib indirection so the import-time skip guard reads cleanly
    from shutil import which

    return which(bin_name)


def _query_local_ssh_agent(sock_path: str, data: bytes) -> bytes | None:
    """Send *data* to the local SSH agent, return its length-prefixed reply.

    Mirrors ``klangk.cli.client.query_local_ssh_agent`` — the wire format
    the CLI's relay uses. Duplicated here because the CLI subpackage is
    isolated from the server package (and these tests are server-side).
    """
    agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    agent.settimeout(_AGENT_PROTOCOL_TIMEOUT)
    try:
        agent.connect(sock_path)
        agent.sendall(data)
        header = b""
        while len(header) < 4:
            chunk = agent.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        msg_len = struct.unpack(">I", header)[0]
        body = b""
        while len(body) < msg_len:
            chunk = agent.recv(msg_len - len(body))
            if not chunk:
                break
            body += chunk
        return header + body
    except OSError:
        return None
    finally:
        agent.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server():
    server = start_server(
        KLANGKD_JWT_SECRET="ssh-agent-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="0",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture
def auth(server):
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture(scope="module")
def local_agent(tmp_path_factory):
    """A throwaway ssh-agent with one generated ed25519 key.

    Yields ``(sock_path, fingerprint)`` where *fingerprint* is the
    ``SHA256:...`` blob ``ssh-add -l`` (and ``ssh-keygen -lf``) report, so the
    assertion is exact. CI runners don't ship a usable agent, so the suite is
    self-contained: ``ssh-agent`` + ``ssh-keygen`` + ``ssh-add``.
    """
    if not _have("ssh-agent", "ssh-keygen", "ssh-add"):
        pytest.skip("ssh-agent / ssh-keygen / ssh-add not on PATH")

    tmp = tmp_path_factory.mktemp("ssh-agent-e2e")
    keyfile = tmp / "id_ed25519"
    out = subprocess.check_output(["ssh-agent", "-s"], text=True)
    match = re.search(r"SSH_AUTH_SOCK=([^;]+);", out)
    pid_match = re.search(r"SSH_AGENT_PID=(\d+);", out)
    assert match and pid_match, f"could not parse ssh-agent output: {out!r}"
    sock_path = match.group(1)
    agent_pid = int(pid_match.group(1))

    agent_env = {**os.environ, "SSH_AUTH_SOCK": sock_path}
    try:
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(keyfile),
                "-C",
                "klangk-e2e",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["ssh-add", str(keyfile)],
            check=True,
            capture_output=True,
            env=agent_env,
        )
        # The fingerprint the container must be able to see.
        fp_out = subprocess.check_output(
            ["ssh-keygen", "-lf", f"{keyfile}.pub"], text=True
        )
        fingerprint = fp_out.split()[1]  # "SHA256:...."
        assert fingerprint.startswith("SHA256:"), fp_out

        yield sock_path, fingerprint
    finally:
        try:
            os.kill(agent_pid, 15)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# WS plumbing
# ---------------------------------------------------------------------------


async def _connect(server, auth, workspace_id):
    """Open a WS, workspace_connect, wait for container_ready.

    Returns ``(ws, received, reader_task)``.
    """
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    received: list[dict] = []

    async def _reader():
        try:
            async for raw in ws:
                try:
                    received.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            pass

    reader_task = asyncio.create_task(_reader())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 90
    while loop.time() < deadline:
        if any(m.get("type") == "container_ready" for m in received):
            return ws, received, reader_task
        await asyncio.sleep(0.2)
    reader_task.cancel()
    await ws.close()
    raise AssertionError("container_ready not received within 90s")


async def _drain(received, predicate, *, timeout=20.0):
    """Wait until *predicate(msg)* holds for some message in *received*.

    Returns the list of matching messages collected so far.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        matched = [m for m in received if predicate(m)]
        if matched:
            return matched
        await asyncio.sleep(0.1)
    return [m for m in received if predicate(m)]


async def _pump_relay(received, ws, agent_sock, *, stop: asyncio.Event):
    """Forward ssh_agent_response → local agent → ssh_agent_data.

    Runs until *stop* is set. Re-scans *received* each tick so it works
    alongside the shared reader that owns the socket.
    """
    seen = 0
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        resps = [m for m in received if m.get("type") == "ssh_agent_response"]
        for m in resps[seen:]:
            seen += 1
            raw = base64.b64decode(m.get("data", ""))
            if not raw:
                continue
            reply = await loop.run_in_executor(
                None, _query_local_ssh_agent, agent_sock, raw
            )
            if reply is not None:
                await ws.send(
                    json.dumps(
                        {
                            "cmd": "ssh_agent_data",
                            "data": base64.b64encode(reply).decode("ascii"),
                        }
                    )
                )
        await asyncio.sleep(0.05)


async def _exec_collect(received, *, start_idx: int, timeout=30.0):
    """Concatenate exec_output in *received* from *start_idx* until exec_exit."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    buf = bytearray()
    code: int | None = None
    # Cursor over *received*: advance past each processed message so a
    # chunk is appended exactly once. Re-scanning the whole slice each
    # poll pass (the old behavior) re-appended every prior chunk once per
    # 0.1s pass — a single "0" line became "0\n0\n0\n0\n" and garbled
    # both the assertions and their failure messages (#2535).
    cursor = start_idx
    while loop.time() < deadline:
        for m in received[cursor:]:
            cursor += 1
            if m.get("type") == "exec_output":
                buf.extend(base64.b64decode(m.get("data", "")))
            elif m.get("type") == "exec_exit":
                code = m.get("code")
                return bytes(buf), code
        await asyncio.sleep(0.1)
    return bytes(buf), code


async def _exec_once(ws, received, *, command: list[str], login: bool = False):
    """Run one exec_start and collect its (stdout, exit_code).

    No agent relay: for commands that don't touch the forwarded socket
    (like the socat count check), so a retry doesn't restart the relay
    under test (#2535).
    """
    # Index-based boundary: only consider messages received AFTER this
    # point as the exec's own output (the reader appends without a
    # timestamp, so time-windowing would misclassify them as old).
    start_idx = len(received)
    await ws.send(
        json.dumps({"cmd": "exec_start", "command": command, "login": login})
    )
    out, code = await _exec_collect(
        received, start_idx=start_idx, timeout=30.0
    )
    return out.decode("utf-8", "replace"), code


async def _run_agent_and_exec(
    ws, received, agent_sock, *, command: list[str], login: bool = False
) -> tuple[str, int | None]:
    """Start the agent relay, run an exec, return its (stdout, exit_code).

    The relay runs concurrently for the whole exec so the container-side
    ``ssh-add`` can reach the host agent through the forwarded socket.
    """
    # (Re)start the relay. ssh_agent_start is idempotent-ish: the server
    # reaps any prior relay on the deterministic path first (#2001), and
    # gates the ssh_agent_started event on the relay socket being bound
    # (#2535), so by the time the exec below runs the new socat is
    # visible in the container.
    await ws.send(json.dumps({"cmd": "ssh_agent_start"}))
    started = await _drain(
        received,
        lambda m: m.get("type") == "ssh_agent_started",
        timeout=20.0,
    )
    assert started, "ssh_agent_started not received"

    stop = asyncio.Event()
    relay_task = asyncio.create_task(
        _pump_relay(received, ws, agent_sock, stop=stop)
    )
    try:
        out, code = await _exec_once(
            ws, received, command=command, login=login
        )
        return out, code
    finally:
        stop.set()
        try:
            await asyncio.wait_for(relay_task, timeout=5)
        except asyncio.TimeoutError:
            relay_task.cancel()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSshAgentForwarding:
    @pytest.mark.asyncio
    async def test_forwarded_agent_visible_in_container(
        self, server, auth, local_agent
    ):
        """A forwarded agent is reachable from inside the container.

        Opens a terminal (which pre-wires SSH_AUTH_SOCK to the deterministic
        per-user path at base-session creation, #2001), starts the agent
        relay, and runs ``ssh-add -l`` via exec. The host key's fingerprint
        must appear, exit 0.
        """
        agent_sock, fingerprint = local_agent
        client = server["client"]
        resp = client.post(
            "/api/v1/workspaces",
            headers=auth["headers"],
            json={"name": "ssh-agent-e2e", "setup_state": "complete"},
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        workspace_id = resp.json()["id"]
        ws, received, reader = await _connect(server, auth, workspace_id)
        try:
            # Open a terminal so the base session is created and the
            # per-user SSH_AUTH_SOCK path is wired into its environment.
            await ws.send(
                json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
            )
            await _drain(
                received,
                lambda m: m.get("type") == "terminal_started",
                timeout=30.0,
            )

            out, code = await _run_agent_and_exec(
                ws, received, agent_sock, command=["ssh-add", "-l"]
            )
            assert code == 0, f"ssh-add -l exited {code}: {out!r}"
            assert fingerprint in out, (
                f"fingerprint {fingerprint} not in ssh-add -l output: {out!r}"
            )
        finally:
            await ws.send(json.dumps({"cmd": "terminal_stop"}))
            reader.cancel()
            try:
                await ws.close()
            except Exception:
                pass
            client.delete(
                f"/api/v1/workspaces/{workspace_id}",
                headers=auth["headers"],
                timeout=30,
            )

    @pytest.mark.asyncio
    async def test_reconnect_does_not_leak_relay(
        self, server, auth, local_agent
    ):
        """Reconnecting a forwarded agent reaps the prior relay (#2001).

        Two back-to-back ``ssh_agent_start`` cycles on the same connection
        must not leave competing ``socat`` listeners on the deterministic
        ``unlink-early`` socket — that race makes the path flicker and
        ``ssh-add`` fail with "No such file or directory". After the second
        start, exactly one socat listens and the key is still reachable.
        """
        agent_sock, fingerprint = local_agent
        client = server["client"]
        resp = client.post(
            "/api/v1/workspaces",
            headers=auth["headers"],
            json={
                "name": "ssh-agent-reconnect-e2e",
                "setup_state": "complete",
            },
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        workspace_id = resp.json()["id"]
        ws, received, reader = await _connect(server, auth, workspace_id)
        try:
            await ws.send(
                json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
            )
            await _drain(
                received,
                lambda m: m.get("type") == "terminal_started",
                timeout=30.0,
            )

            # Cycle 1: start relay + ssh-add -l must succeed.
            out1, code1 = await _run_agent_and_exec(
                ws, received, agent_sock, command=["ssh-add", "-l"]
            )
            assert code1 == 0 and fingerprint in out1, (code1, out1)
            await ws.send(json.dumps({"cmd": "ssh_agent_stop"}))
            await _drain(
                received,
                lambda m: m.get("type") == "ssh_agent_stopped",
                timeout=10.0,
            )

            # Cycle 2 (reconnect): the prior relay must be reaped, not leaked.
            out2, code2 = await _run_agent_and_exec(
                ws, received, agent_sock, command=["ssh-add", "-l"]
            )
            assert code2 == 0 and fingerprint in out2, (code2, out2)

            # Exactly one socat listening on the per-user path: a leak
            # would show >= 2 (pgrep -c prints one number; login shell for
            # PATH). The server gates ssh_agent_started on the socket
            # being bound (#2535), so a count of 0 should no longer
            # happen — but if one slips through (readiness timeout under
            # load), retry briefly instead of failing: the assertion
            # under test is "no leak", and a leak shows >= 2 on the very
            # first check, not after a delay.
            out3, code3 = await _run_agent_and_exec(
                ws,
                received,
                agent_sock,
                command=["bash", "-lc", "pgrep -c socat || true"],
                login=True,
            )
            assert code3 == 0, (code3, out3)
            count = _socat_count(out3)
            deadline = time.monotonic() + 10.0
            while count == 0 and time.monotonic() < deadline:
                # Retry with a bare exec (no ssh_agent_start) so the
                # retry doesn't itself restart the relay under test.
                out3, code3 = await _exec_once(
                    ws,
                    received,
                    command=["bash", "-lc", "pgrep -c socat || true"],
                    login=True,
                )
                assert code3 == 0, (code3, out3)
                count = _socat_count(out3)
                if count == 0:
                    await asyncio.sleep(0.5)
            assert count == 1, (
                f"expected exactly 1 socat after reconnect, got {count} "
                f"(relay leak): {out3!r}"
            )
        finally:
            await ws.send(json.dumps({"cmd": "terminal_stop"}))
            reader.cancel()
            try:
                await ws.close()
            except Exception:
                pass
            client.delete(
                f"/api/v1/workspaces/{workspace_id}",
                headers=auth["headers"],
                timeout=30,
            )
