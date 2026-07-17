"""E2E tests for terminal window independence across CLI and web sessions.

Constraints tested:
1. Within a given web tab or klangkc shell, the displayed window never
   changes unless the user explicitly switches.
2. klangkc shell ws = visiting the default terminal (window 0).
3. klangkc shell ws NAME = creating/attaching to a named window.
4. Web and CLI viewing the same named window see the same tmux window.
5. Terminal window mutations (create, close, rename) are broadcast to
   all connections for the same user.

Run with: devenv shell -- test-cli-e2e -k TestTerminalWindows
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

import httpx
import pytest
import websockets

from klangk.model import free_port
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "klangkd-tests", "e2e-tests"
    ),
)
from _e2e_env import clean_env
from pathlib import Path

logger = logging.getLogger(__name__)

PORT = str(free_port())
WS_NAME = "e2e-twintest"


def _run(args, timeout=120, input=None, **kwargs):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
        **kwargs,
    )


def _start_server(data_dir):
    state_dir = tempfile.mkdtemp(prefix="klangk-tw-e2e-state-")
    env = clean_env(
        KLANGK_PORT=PORT,
        KLANGK_DATA_DIR=data_dir,
        KLANGK_STATE_DIR=state_dir,
        KLANGK_JWT_SECRET="tw-e2e-secret",
        KLANGK_PREVENT_INSECURE_JWT_SECRET="",
        KLANGK_DEFAULT_USER="test@example.com",
        KLANGK_DEFAULT_PASSWORD="testpass",
        KLANGK_TEST_MODE="1",
        KLANGK_IDLE_TIMEOUT_SECONDS="300",
        KLANGK_PORT_RANGE_START=str(free_port()),
        LOGFIRE_TOKEN="",
    )
    log_path = os.path.join(data_dir, "server.log")
    log_file = open(log_path, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            "python3",
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "klangkd-tests",
                "e2e-tests",
                "runtestserver.py",
            ),
            "--host",
            "0.0.0.0",
            "--port",
            PORT,
            "--ws-max-size",
            "16777216",
            "--ws-ping-interval",
            "20",
            "--ws-ping-timeout",
            "20",
        ],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    proc._log_file = log_file
    proc._log_path = log_path
    base_url = f"http://localhost:{PORT}"
    for _ in range(60):
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        log_file.close()
        stdout = open(log_path).read() if os.path.exists(log_path) else ""
        raise RuntimeError(f"Server failed to start:\n{stdout}")
    return proc, base_url, env


def _stop_server(proc, data_dir):
    if hasattr(proc, "_log_file"):
        proc._log_file.close()
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    # Instance-scoped cleanup: only remove containers THIS test server
    # started (label=klangk.instance=<id>), never another suite's or xdist
    # worker's. The old ``label=klangk.managed=true`` filter was a cross-run
    # hazard once suites could run concurrently (#1393). The ID lives in
    # ``<data_dir>/instance-id`` (written by klangkd at startup, #1553); read
    # it directly rather than shelling out to a console script (#1565).
    _id_file = Path(data_dir) / "instance-id"
    instance_id = _id_file.read_text().strip() if _id_file.exists() else ""
    if instance_id:
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


def _login(base_url, env):
    _run(
        [
            "klangkc",
            "login",
            base_url,
            "test@example.com",
            "--password-file",
            "-",
        ],
        input="testpass\n",
        env=env,
    )


def _get_token(base_url):
    r = httpx.post(
        f"{base_url}/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _cli_check_window(ws_name, env, timeout=25):
    """Use expect to connect CLI to workspace, check tmux window, disconnect."""
    script = f"""
set timeout {timeout}
log_user 0
spawn klangkc shell {ws_name}
expect {{
    -re {{\\$ }} {{}}
    timeout {{ puts "TIMEOUT"; exit 1 }}
}}
sleep 2
send "echo W_S; tmux display-message -p '#I:#W'; echo W_E\\r"
expect {{
    -re {{W_S\\r?\\n(\\d+:\\S+)\\r?\\nW_E}} {{
        puts $expect_out(1,string)
    }}
    timeout {{ puts "TIMEOUT"; exit 1 }}
}}
send "\\r"
sleep 0.5
send "~."
expect {{ eof {{}} timeout {{ close }} }}
wait
"""
    result = subprocess.run(
        ["expect", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout + 10,
        env=env,
    )
    return result.stdout.strip()


def _cli_hold_and_check(ws_name, window_name, hold_seconds, env):
    """Start CLI shell, check window before and after a hold period."""
    if window_name:
        cmd = f"klangkc shell {ws_name} {window_name}"
    else:
        cmd = f"klangkc shell {ws_name}"
    script = f"""
set timeout 30
log_user 0
spawn {cmd}
expect {{
    -re {{\\$ }} {{}}
    timeout {{ puts "TIMEOUT:prompt"; exit 1 }}
}}
sleep 2
send "echo BS; tmux display-message -p '#I:#W'; echo BE\\r"
expect {{
    -re {{BS\\r?\\n(\\d+:\\S+)\\r?\\nBE}} {{
        set before $expect_out(1,string)
    }}
    timeout {{ puts "TIMEOUT:before"; exit 1 }}
}}
puts "BEFORE=$before"
sleep {hold_seconds}
send "echo AS; tmux display-message -p '#I:#W'; echo AE\\r"
expect {{
    -re {{AS\\r?\\n(\\d+:\\S+)\\r?\\nAE}} {{
        set after $expect_out(1,string)
    }}
    timeout {{ puts "TIMEOUT:after"; exit 1 }}
}}
puts "AFTER=$after"
send "\\r"
sleep 0.5
send "~."
expect {{ eof {{}} timeout {{ close }} }}
wait
"""
    result = subprocess.run(
        ["expect", "-c", script],
        capture_output=True,
        text=True,
        timeout=hold_seconds + 40,
        env=env,
    )
    before = after = None
    for line in result.stdout.strip().splitlines():
        if line.startswith("BEFORE="):
            before = line.split("=", 1)[1]
        elif line.startswith("AFTER="):
            after = line.split("=", 1)[1]
    return before, after


class _WebSession:
    """Simulates a web UI WebSocket connection."""

    def __init__(self, base_url, token, workspace_id):
        self.base_url = base_url
        self.token = token
        self.workspace_id = workspace_id
        self.ws = None
        self.windows = []
        self.selected_window_id = None
        self._ctx = None

    async def connect(self):
        ws_url = self.base_url.replace("http://", "ws://")
        self._ctx = websockets.connect(
            f"{ws_url}/ws?token={self.token}",
            max_size=16 * 1024 * 1024,
        )
        self.ws = await self._ctx.__aenter__()
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "workspace_connect",
                    "workspaceId": self.workspace_id,
                }
            )
        )
        await self._drain_until("container_ready")
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "terminal_start",
                    "cols": 80,
                    "rows": 24,
                    "browser_id": "e2e-web",
                }
            )
        )
        await self._drain_until("terminal_windows")
        if self.windows:
            self.selected_window_id = self.windows[0]["id"]

    async def _drain_until(self, target_type, timeout=60):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("type") == "terminal_windows":
                self.windows = msg.get("windows", [])
            if msg.get("type") == target_type:
                return msg
        raise TimeoutError(f"Waiting for {target_type}")

    async def drain_pending(self, timeout=1.0):
        try:
            while True:
                raw = await asyncio.wait_for(
                    self.ws.recv(),
                    timeout=timeout,
                )
                msg = json.loads(raw)
                if msg.get("type") == "terminal_windows":
                    self.windows = msg.get("windows", [])
        except (asyncio.TimeoutError, TimeoutError):
            pass

    async def create_window(self, name):
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "terminal_new_window",
                    "name": name,
                }
            )
        )
        await self._drain_until("terminal_windows", timeout=10)
        new_win = next(
            (w for w in self.windows if w["name"] == name),
            None,
        )
        if new_win:
            self.selected_window_id = new_win["id"]
            await self.ws.send(
                json.dumps(
                    {
                        "cmd": "terminal_select_window",
                        "window_id": new_win["id"],
                    }
                )
            )

    async def select_window(self, window_id):
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "terminal_select_window",
                    "window_id": window_id,
                }
            )
        )
        self.selected_window_id = window_id
        await asyncio.sleep(0.5)

    async def close_window(self, index):
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "terminal_close_window",
                    "index": index,
                }
            )
        )
        await self._drain_until("terminal_windows", timeout=10)

    async def disconnect(self):
        if self._ctx:
            await self._ctx.__aexit__(None, None, None)
            self._ctx = None
            self.ws = None


class TestTerminalWindows:
    """Terminal window independence between CLI and web sessions."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def _dedicated_server(tmp_path_factory, request):
        data_dir = tempfile.mkdtemp(prefix="klangk-tw-e2e-")
        proc, base_url, server_env = _start_server(data_dir)
        config_dir = tmp_path_factory.mktemp("klangk-tw-config")
        env = clean_env(HOME=str(config_dir))
        (config_dir / ".config" / "klangk").mkdir(parents=True)
        _login(base_url, env)
        token = _get_token(base_url)
        request.cls._env = env
        request.cls._base_url = base_url
        request.cls._token = token
        yield
        _stop_server(proc, data_dir)

    @pytest.fixture(autouse=True)
    def _fresh_workspace(self):
        """Create a fresh workspace for each test."""
        _run(["klangkc", "rm", WS_NAME], env=self._env)
        result = _run(["klangkc", "create", WS_NAME], env=self._env)
        assert result.returncode == 0, result.stderr
        # Resolve full ID via API
        r = httpx.get(
            f"{self._base_url}/api/v1/workspaces",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        self._ws_id = None
        for ws in r.json():
            if ws["name"] == WS_NAME:
                self._ws_id = ws["id"]
                break
        assert self._ws_id, f"Workspace {WS_NAME} not found after create"
        # Warm up container
        _run(
            ["klangkc", "exec", WS_NAME, "true"],
            env=self._env,
            timeout=120,
        )
        yield
        _run(["klangkc", "rm", WS_NAME], env=self._env)

    def test_web_tab_switch_doesnt_move_cli(self):
        """Clicking a different tab in web UI doesn't change CLI window."""

        async def _test():
            # CLI on default, hold 15s
            loop = asyncio.get_event_loop()
            cli = loop.run_in_executor(
                None,
                _cli_hold_and_check,
                WS_NAME,
                None,
                15,
                self._env,
            )
            await asyncio.sleep(5)

            web = _WebSession(self._base_url, self._token, self._ws_id)
            await web.connect()
            await web.create_window("tab2")
            tab2 = next(
                (w for w in web.windows if w["name"] == "tab2"),
                None,
            )
            if tab2:
                await web.select_window(tab2["id"])
            await asyncio.sleep(2)
            await web.disconnect()

            before, after = await cli
            assert before == after, f"CLI moved from {before} to {after}"

        asyncio.run(_test())

    def test_named_cli_doesnt_move_default_cli(self):
        """Opening klangkc shell ws NAME doesn't move default CLI."""
        script = f"""
set timeout 30
log_user 0
spawn klangkc shell {WS_NAME}
set s1 $spawn_id
expect -re {{\\$ }}
sleep 3
send "echo B1S; tmux display-message -p '#I:#W'; echo B1E\\r"
expect -re {{B1S\\r?\\n(\\d+:\\S+)\\r?\\nB1E}}
set b1 $expect_out(1,string)

spawn klangkc shell {WS_NAME} named1
set s2 $spawn_id
expect -re {{\\$ }}
sleep 3

set spawn_id $s1
send "echo A1S; tmux display-message -p '#I:#W'; echo A1E\\r"
expect -re {{A1S\\r?\\n(\\d+:\\S+)\\r?\\nA1E}}
set a1 $expect_out(1,string)

foreach sid [list $s1 $s2] {{
    set spawn_id $sid
    send "\\r"; sleep 0.3; send "~."
    expect {{ eof {{}} timeout {{ close }} }}
    wait
}}
puts "BEFORE=$b1"
puts "AFTER=$a1"
"""
        result = _run(
            ["expect", "-c", script],
            env=self._env,
            timeout=60,
        )
        before = after = None
        for line in result.stdout.strip().splitlines():
            if line.startswith("BEFORE="):
                before = line.split("=", 1)[1]
            elif line.startswith("AFTER="):
                after = line.split("=", 1)[1]
        assert before == after, f"Default CLI moved from {before} to {after}"

    def test_named_cli_doesnt_move_web(self):
        """CLI creating a named window doesn't change web's selected tab."""

        async def _test():
            web = _WebSession(self._base_url, self._token, self._ws_id)
            await web.connect()
            initial = web.selected_window_id

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                _cli_hold_and_check,
                WS_NAME,
                "fromcli",
                5,
                self._env,
            )

            await web.drain_pending()
            assert web.selected_window_id == initial, (
                f"Web moved from {initial} to {web.selected_window_id}"
            )
            # The new window should appear in the list
            names = [w["name"] for w in web.windows]
            assert "fromcli" in names, f"'fromcli' not in web windows: {names}"
            await web.disconnect()

        asyncio.run(_test())

    def test_web_close_tab_doesnt_move_cli(self):
        """Web closing a tab doesn't affect CLI on a different window."""

        async def _test():
            # Create an extra window first
            web_setup = _WebSession(
                self._base_url,
                self._token,
                self._ws_id,
            )
            await web_setup.connect()
            await web_setup.create_window("extra")
            await web_setup.disconnect()
            await asyncio.sleep(1)

            loop = asyncio.get_event_loop()
            cli = loop.run_in_executor(
                None,
                _cli_hold_and_check,
                WS_NAME,
                None,
                15,
                self._env,
            )
            await asyncio.sleep(5)

            web = _WebSession(self._base_url, self._token, self._ws_id)
            await web.connect()
            extra = next(
                (w for w in web.windows if w["name"] == "extra"),
                None,
            )
            if extra:
                await web.close_window(extra["index"])
            await asyncio.sleep(2)
            await web.disconnect()

            before, after = await cli
            assert before == after, f"CLI moved from {before} to {after}"

        asyncio.run(_test())

    def test_rapid_tab_switching_doesnt_move_cli(self):
        """Rapidly switching web tabs doesn't change CLI window."""

        async def _test():
            # Create extra windows
            web_setup = _WebSession(
                self._base_url,
                self._token,
                self._ws_id,
            )
            await web_setup.connect()
            await web_setup.create_window("rapid1")
            await web_setup.create_window("rapid2")
            await web_setup.disconnect()
            await asyncio.sleep(1)

            loop = asyncio.get_event_loop()
            cli = loop.run_in_executor(
                None,
                _cli_hold_and_check,
                WS_NAME,
                None,
                20,
                self._env,
            )
            await asyncio.sleep(5)

            web = _WebSession(self._base_url, self._token, self._ws_id)
            await web.connect()
            for _ in range(5):
                for w in web.windows:
                    await web.select_window(w["id"])
                    await asyncio.sleep(0.2)
            await web.disconnect()

            before, after = await cli
            assert before == after, f"CLI moved from {before} to {after}"

        asyncio.run(_test())

    def test_cli_reconnect_to_named_window(self):
        """CLI disconnect/reconnect to same named window works."""
        w1 = _cli_check_window(WS_NAME + " persist", self._env)
        assert "persist" in w1, f"First connect: {w1}"
        w2 = _cli_check_window(WS_NAME + " persist", self._env)
        assert "persist" in w2, f"Second connect: {w2}"
        # Both should report the same window name
        name1 = w1.split(":", 1)[1] if ":" in w1 else w1
        name2 = w2.split(":", 1)[1] if ":" in w2 else w2
        assert name1 == name2

    def test_web_sees_cli_created_windows(self):
        """Web UI shows windows created by CLI."""

        async def _test():
            _cli_check_window(WS_NAME + " cliwin1", self._env)
            _cli_check_window(WS_NAME + " cliwin2", self._env)

            web = _WebSession(self._base_url, self._token, self._ws_id)
            await web.connect()
            names = [w["name"] for w in web.windows]
            assert "cliwin1" in names, f"Missing cliwin1: {names}"
            assert "cliwin2" in names, f"Missing cliwin2: {names}"
            await web.disconnect()

        asyncio.run(_test())

    def test_cli_and_web_same_named_window(self):
        """CLI and web on same named window share the same tmux window."""

        async def _test():
            web = _WebSession(self._base_url, self._token, self._ws_id)
            await web.connect()
            await web.create_window("shared")
            shared = next(
                (w for w in web.windows if w["name"] == "shared"),
                None,
            )
            assert shared is not None
            web_wid = shared["id"]

            # CLI connects to same named window, check tmux window_id
            script = f"""
set timeout 25
log_user 0
spawn klangkc shell {WS_NAME} shared
expect {{
    -re {{\\$ }} {{}}
    timeout {{ puts "TIMEOUT"; exit 1 }}
}}
sleep 2
send "echo WS; tmux display-message -p '#{{window_id}}:#{{window_name}}'; echo WE\\r"
expect {{
    -re {{WS\\r?\\n(@\\d+:\\S+)\\r?\\nWE}} {{
        puts "WID=$expect_out(1,string)"
    }}
    timeout {{ puts "TIMEOUT"; exit 1 }}
}}
send "\\r"
sleep 0.5
send "~."
expect {{ eof {{}} timeout {{ close }} }}
wait
"""
            result = _run(
                ["expect", "-c", script],
                env=self._env,
                timeout=35,
            )
            cli_wid = None
            for line in result.stdout.strip().splitlines():
                if line.startswith("WID="):
                    cli_wid = line.split("=", 1)[1].split(":")[0]

            assert cli_wid == web_wid, f"CLI on {cli_wid} but web on {web_wid}"
            await web.disconnect()

        asyncio.run(_test())

    def test_cli_default_is_window_zero(self):
        """klangkc shell ws (no arg) lands on window 0."""
        window = _cli_check_window(WS_NAME, self._env)
        assert window.startswith("0:"), f"Expected 0:*, got {window}"

    def test_two_web_sessions_independent(self):
        """Two web sessions have independent tab selection."""

        async def _test():
            web_a = _WebSession(
                self._base_url,
                self._token,
                self._ws_id,
            )
            await web_a.connect()
            await web_a.create_window("tabA")
            tab_a = next(
                (w for w in web_a.windows if w["name"] == "tabA"),
                None,
            )

            web_b = _WebSession(
                self._base_url,
                self._token,
                self._ws_id,
            )
            await web_b.connect()

            if tab_a:
                await web_a.select_window(tab_a["id"])

            await web_b.drain_pending()
            assert web_a.selected_window_id != web_b.selected_window_id, (
                f"Both on {web_a.selected_window_id}"
            )
            await web_a.disconnect()
            await web_b.disconnect()

        asyncio.run(_test())
