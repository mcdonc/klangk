"""E2E test: openclaw sandbox writes all .bashrc exports before slow setup steps.

Guards the #1039 fix. ``sandboxes/openclaw/setup.sh`` appends every env
export the default_command depends on (``NVM_DIR``, ``/openclaw/bin`` on
``PATH``, ``OPENCLAW_HOME``) to ``~/.bashrc`` in one block at the very
top of setup, before the long ``npm install -g openclaw``. Previously
``OPENCLAW_HOME`` was appended near the end, so a shell spawned mid-setup
(e.g. the ``default-cmd`` pane from an early ``terminal_start`` -- the
#1033 race) inherited ``PATH`` but not ``OPENCLAW_HOME``; with
``OPENCLAW_HOME`` unset, ``openclaw gateway`` looked for config at
``$HOME/.openclaw`` instead of ``/openclaw/.openclaw`` and reported
"Missing config".

How the test exercises this deterministically: ``setup.sh`` blocks while
a sentinel file (``/openclaw/.klangk-test-pause``, i.e. on the bind
mount) exists. The sentinel is placed right after the consolidated export
block and before the slow install, so while setup is parked there the
test reads ``~/.bashrc`` straight off the host filesystem and asserts
the export is already present. On a regression that moves the
``OPENCLAW_HOME`` append back to the end of setup, ``~/.bashrc`` lacks it
at this point and the test fails. Releasing the sentinel lets the real
``npm install -g openclaw`` run, and the test confirms the binary lands
on the mount.

This reads files directly from the host (the per-user home and the
``/openclaw`` mount both live under the server data dir / the sandbox
dir). It deliberately avoids ``klangkc exec``: exec runs a
non-interactive shell that short-circuits ``~/.bashrc`` at its
interactivity guard (#1041), so it would not see these exports and would
give a false negative.

Run locally (the workspace image must be built first):

    devenv shell -- klangk:build-workspace-image
    devenv shell -- pytest sandboxes/tests/openclaw -v -p no:xdist --no-cov

Gated to its own workflow (sandbox-openclaw-e2e) on changes to the
sandbox, so it never slows the regular CLI/backend e2e suites. Requires
network (real ``npm install``).
"""

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import httpx
import pytest
import websockets

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SANDBOX_DIR = os.path.join(REPO_ROOT, "sandboxes", "openclaw")
# setup.sh blocks while this file exists on the mount (/openclaw is the
# bind-mounted sandbox dir).
SENTINEL = os.path.join(SANDBOX_DIR, ".klangk-test-pause")

WS = "e2e-openclaw-setup"
# Second workspace pointed at the SAME /openclaw mount. openclaw is
# already installed there after WS's setup ran, so WS2's setup SKIPS the
# install but must still write a complete per-workspace ~/.bashrc. This
# is the shared-mount + per-workspace-.bashrc interaction at the heart
# of #1039 -- a regression that moves an export inside the install-skip
# guard breaks WS2 permanently (not just a race) while WS may still pass.
WS2 = "e2e-openclaw-setup-2"
# Third workspace at the same mount, used by the #1033 visitor test.
# Like WS2 its setup skips the install (mount already populated) and
# pauses at the sentinel, so the test can drive a VISITOR terminal_start
# while setup is genuinely mid-flight -- the exact #1033 race.
WS3 = "e2e-openclaw-setup-3"
# Own port + instance id so it never collides with other e2e suites
# (cli-e2e 18995, autostart 18996/18997).
PORT = "18998"
INSTANCE = "openclaw-setup-e2e"
EMAIL = "test@example.com"
PASSWORD = "testpass"
# Real npm install of openclaw (nvm + node + global package) can take a
# while, especially on CI.
SETUP_TIMEOUT = 900


def _run(args, timeout=120, input=None, env=None, **kwargs):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
        env=env,
        **kwargs,
    )


def _login(base_url):
    """Log in and return (token, user_id)."""
    r = httpx.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    # The login response carries only the token; /my-permissions returns
    # the user id (there is no /me endpoint).
    headers = {"Authorization": f"Bearer {token}"}
    mp = httpx.get(f"{base_url}/api/v1/my-permissions", headers=headers, timeout=30)
    mp.raise_for_status()
    user_id = mp.json()["user_id"]
    return token, user_id


def _start_server(data_dir, port, instance_id, extra_env=None):
    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "openclaw-e2e-test-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": EMAIL,
        "KLANGK_DEFAULT_PASSWORD": PASSWORD,
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": instance_id,
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        "KLANGK_PORT_RANGE_START": "9000",
        "KLANGK_ALLOW_AUTOSTART": "1",
        "LOGFIRE_TOKEN": "",
        **(extra_env or {}),
    }
    log_path = os.path.join(data_dir, "server.log")
    log_file = open(log_path, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            "uvicorn",
            "klangk_backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
        cwd=os.path.join(REPO_ROOT, "src", "backend"),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log_file = log_file  # type: ignore[attr-defined]
    proc._log_path = log_path  # type: ignore[attr-defined]
    base_url = f"http://localhost:{port}"
    for _ in range(90):
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return proc, base_url
        except Exception:
            pass
        time.sleep(1)
    proc.kill()
    log_file.close()
    stdout = open(log_path).read() if os.path.exists(log_path) else ""
    raise RuntimeError(f"Server failed to start:\n{stdout}")


def _stop_server(proc, data_dir, instance_id):
    if hasattr(proc, "_log_file"):
        proc._log_file.close()
    # SIGKILL children too (uvicorn spawns a worker); TERM alone can
    # leave the port bound if a child is mid-syscall.
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
    res = subprocess.run(
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
    if res.stdout.strip():
        subprocess.run(
            ["podman", "rm", "-f", *res.stdout.strip().split()],
            capture_output=True,
        )
    shutil.rmtree(data_dir, ignore_errors=True)


def _force_kill_port(port):
    """SIGKILL any process bound to *port* (TERM is not always enough).

    Belt-and-suspenders cleanup so a crashed run can never leave a
    server squatting on the port and fool the next run's health check.
    """
    try:
        out = subprocess.run(["ss", "-tlnHp"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return
    for line in out.splitlines():
        if f":{port}" not in line:
            continue
        # "pid=1234" in the ss output.
        for tok in line.split():
            if tok.startswith("pid="):
                try:
                    os.kill(int(tok[4:].split(",")[0]), 9)
                except (ProcessLookupError, ValueError):
                    pass


def _workspace_id(base_url, token, name):
    r = httpx.get(
        f"{base_url}/api/v1/workspaces",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json()
    if isinstance(items, dict):
        items = items.get("workspaces") or items.get("data") or []
    for ws in items:
        if ws.get("name") == name:
            return ws["id"]
    raise LookupError(f"workspace {name!r} not found")


def _container_up(base_url, token, ws_id):
    r = httpx.get(
        f"{base_url}/api/v1/workspaces/{ws_id}/status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return bool(r.json().get("container_id"))


def _owning_bashrc(data_dir, user_id, ws_id):
    """Path to the owning user's ~/.bashrc on the host for *ws_id*.

    The per-workspace home is bind-mounted at /home; the owning user's
    real home is ``<data>/workspaces/<uid>/home/<ws_id>/.users/<uid>/``
    (home_path is keyed by workspace id, so this resolves directly).
    """
    return os.path.join(
        data_dir,
        "workspaces",
        user_id,
        "home",
        ws_id,
        ".users",
        user_id,
        ".bashrc",
    )


async def _visitor_terminal_env(base_url, token, ws_id, var, timeout=25.0):
    """Connect as a VISITOR, fire ``terminal_start``, return ``$var``.

    Emulates the #1033 scenario: a second WebSocket connection (not the
    backend's own setup connection) sends ``terminal_start`` while setup
    is still running. The backend spawns the per-user tmux base session
    plus the ``default-cmd`` window; the visitor's own interactive pane
    (window 0) sources ``~/.bashrc`` at that instant and sits at a clean
    prompt. We type a probe into window 0 and read back ``$var`` -- this
    is the live environment of a shell spawned mid-setup via the visitor
    path, not the frozen ``/proc/environ`` (which predates .bashrc) and
    not the .bashrc file contents.

    Returns ``(value, windows)`` where *windows* is the terminal_windows
    list (so callers can assert the default-cmd pane was spawned).
    """
    ws_url = base_url.replace("http://", "ws://") + "/ws"
    async with websockets.connect(f"{ws_url}?token={token}", max_size=2**24) as ws:
        await ws.send(json.dumps({"cmd": "workspace_connect", "workspaceId": ws_id}))
        windows = []
        # Drain until workspace_ready.
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("visitor: no workspace_ready")
            msg = json.loads(await asyncio.wait_for(ws.recv(), remaining))
            if msg.get("type") == "workspace_ready":
                break
            if msg.get("type") == "error":
                raise ConnectionError(f"visitor connect failed: {msg}")

        await ws.send(
            json.dumps(
                {
                    "cmd": "terminal_start",
                    "cols": 80,
                    "rows": 24,
                    "browser_id": "e2e-visitor",
                }
            )
        )
        # Drain until terminal_started, capturing the window list.
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("visitor: no terminal_started")
            msg = json.loads(await asyncio.wait_for(ws.recv(), remaining))
            if msg.get("type") == "terminal_started":
                continue
            if msg.get("type") == "terminal_windows":
                windows = msg.get("windows", [])
                break
            if msg.get("type") == "error":
                raise ConnectionError(f"visitor terminal_start: {msg}")

        # Probe window 0's live env. Retry the probe until the marker
        # shows up (the shell may need a moment to present a prompt).
        left, right = "MKOPEN", "MKCLOSE"
        cmd = f"printf '{left}%s{right}\\n' \"${var}\"\r"
        pattern = re.compile(re.escape(left) + r"(.*?)" + re.escape(right))
        deadline = asyncio.get_event_loop().time() + timeout
        buf = ""
        sent = False
        while asyncio.get_event_loop().time() < deadline:
            if not sent:
                await ws.send(json.dumps({"cmd": "terminal_input", "data": cmd}))
                sent = True
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                # Re-send the probe in case the shell wasn't ready.
                sent = False
                continue
            msg = json.loads(raw)
            if msg.get("type") == "terminal_output":
                buf += msg.get("data", "")
                # The echoed command line contains "%s" between the
                # markers; the real output contains the expanded value.
                for m in pattern.finditer(buf):
                    val = m.group(1)
                    if val != "%s":
                        return val, windows
        raise AssertionError(
            f"visitor probe for ${var} never returned a marker within "
            f"{timeout}s. Output:\n{buf!r}"
        )


class TestOpenclawSetupBashrcExports:
    """Real openclaw sandbox: .bashrc exports precede the slow install."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def server(tmp_path_factory, request):
        data_dir = tempfile.mkdtemp(prefix="klangk-openclaw-e2e-")
        proc, base_url = _start_server(data_dir, PORT, INSTANCE)
        config_dir = tmp_path_factory.mktemp("klangk-openclaw-config")
        env = {**os.environ, "HOME": str(config_dir)}
        os.makedirs(config_dir / ".config" / "klangk", exist_ok=True)
        _run(
            ["klangkc", "login", base_url, EMAIL, "--password-file", "-"],
            input=PASSWORD + "\n",
            env=env,
        )
        token, user_id = _login(base_url)
        request.cls._env = env
        request.cls._base_url = base_url
        request.cls._token = token
        request.cls._user_id = user_id
        request.cls._data_dir = data_dir
        # Clean the shared /openclaw mount so every pytest run is
        # deterministic: WS gets a full install, WS2 reuses it. These
        # are gitignored install artifacts, never committed.
        shutil.rmtree(os.path.join(SANDBOX_DIR, ".nvm"), ignore_errors=True)
        shutil.rmtree(os.path.join(SANDBOX_DIR, ".openclaw"), ignore_errors=True)
        shutil.rmtree(os.path.join(SANDBOX_DIR, "bin"), ignore_errors=True)
        # Drop the sentinel BEFORE starting sandbox so setup.sh blocks.
        if os.path.exists(SENTINEL):
            os.remove(SENTINEL)
        with open(SENTINEL, "w") as f:
            f.write("1\n")
        yield
        # Hard cleanup: kill -9 any straggling processes (TERM is not
        # enough when a podman/uvicorn child is mid-syscall).
        if os.path.exists(SENTINEL):
            os.remove(SENTINEL)
        _stop_server(proc, data_dir, INSTANCE)
        _force_kill_port(PORT)

    def _capture_sandbox(self, sandbox_proc):
        """Return (stdout, stderr) from the sandbox subprocess if finished."""
        if sandbox_proc.poll() is None:
            return ("", "(still running)")
        out = sandbox_proc.stdout.read() if sandbox_proc.stdout else ""
        err = sandbox_proc.stderr.read() if sandbox_proc.stderr else ""
        return (out, err)

    def _server_log_tail(self, n=40):
        """Tail of the server log, for failure diagnostics."""
        log_path = os.path.join(self._data_dir, "server.log")
        if not os.path.exists(log_path):
            return "(no server.log)"
        with open(log_path) as f:
            return "".join(f.readlines()[-n:])

    def test_bashrc_has_openclaw_home_before_slow_install(self):
        """~/.bashrc contains OPENCLAW_HOME before the npm install runs.

        This is the #1039 invariant: every export the default_command
        depends on is written up front, so a shell spawned at any point
        during setup (here, held by the sentinel after the export block
        but before the npm install) sources a complete ~/.bashrc. Under
        the original bug OPENCLAW_HOME was appended after the install, so
        .bashrc would be missing it at this point.
        """
        env = self._env

        # Run the sandbox in the background; it blocks on the sentinel
        # right after writing the consolidated .bashrc exports.
        sandbox_proc = subprocess.Popen(
            ["klangkc", "sandbox", WS, SANDBOX_DIR],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            # Wait for the container to be up (setup is running, held by
            # the sentinel after the export block).
            ws_id = self._await_container()
            bashrc_path = self._await_setup_exports(ws_id)
            with open(bashrc_path) as f:
                bashrc = f.read()

            # The invariant under test: OPENCLAW_HOME is already in
            # .bashrc while setup is still parked before the slow install.
            assert 'export OPENCLAW_HOME="/openclaw"' in bashrc, (
                "OPENCLAW_HOME missing from ~/.bashrc before the slow "
                "install step -- the #1039 export ordering regressed; a "
                "shell spawned during setup cannot locate openclaw "
                "config.\n.bashrc:\n" + bashrc
            )
            assert 'export PATH="/openclaw/bin:$PATH"' in bashrc

            # Release setup.sh and let the real openclaw install finish.
            os.remove(SENTINEL)
            assert sandbox_proc.wait(timeout=SETUP_TIMEOUT) == 0, (
                "klangkc sandbox failed:\n" + (sandbox_proc.stdout.read() or "")
            )

            # The real install ran: the openclaw binary lands on the
            # /openclaw mount (== the sandbox dir on the host).
            assert glob.glob(
                os.path.join(
                    SANDBOX_DIR, ".nvm", "versions", "node", "v*", "bin", "openclaw"
                )
            ), "openclaw binary not installed on the mount after setup"
        finally:
            if os.path.exists(SENTINEL):
                os.remove(SENTINEL)
            # Hard-kill the sandbox subprocess and dump its output if it
            # died unexpectedly (it streams create errors to stdout).
            if sandbox_proc.poll() is None:
                sandbox_proc.kill()
            else:
                out, _ = self._capture_sandbox(sandbox_proc)
                if out:
                    print(f"--- klangkc sandbox output ---\n{out}")

    def test_second_workspace_reuses_install_but_writes_own_bashrc(self):
        """A second workspace at the same /openclaw mount SKIPS the install
        (openclaw is already there) yet must still write a complete
        per-workspace ~/.bashrc.

        This is the shared-mount + per-workspace-.bashrc interaction at
        the heart of #1039. ``~/.bashrc`` is fresh per workspace (the
        owning user's home is per-workspace), while the ``/openclaw``
        mount (nvm/node/openclaw) is shared. A regression that moves an
        export INSIDE the install-skip guard breaks this workspace
        PERMANENTLY (not just a race), because the guard's body never
        runs when the install is skipped. The full-install test above
        (WS) can't see this -- it always runs the guard body.

        Depends on the full-install test having populated the mount
        first (pytest runs them in definition order; the whole suite
        runs together in CI / locally).
        """
        # Precondition: openclaw must already be on the shared mount
        # (put there by the full-install test). If this is run in
        # isolation, fail fast with a clear message rather than doing a
        # misleading full install that would hide the skip-path bug.
        assert glob.glob(
            os.path.join(
                SANDBOX_DIR, ".nvm", "versions", "node", "v*", "bin", "openclaw"
            )
        ), (
            "openclaw not on the shared mount -- run the full-install test "
            "first so this test can exercise the install-skip path"
        )

        # No sentinel: WS2's setup runs to completion. The install is
        # skipped (openclaw already on the mount), so this is fast.
        sandbox_proc = subprocess.Popen(
            ["klangkc", "sandbox", WS2, SANDBOX_DIR],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            rc = sandbox_proc.wait(timeout=SETUP_TIMEOUT)
            out = sandbox_proc.stdout.read() or ""
            assert rc == 0, f"klangkc sandbox (WS2) failed:\n{out}"

            # The install was genuinely skipped -- proving we're on the
            # skip path, not a vacuous pass where a regression re-ran
            # the full install and wrote .bashrc that way.
            assert "openclaw already installed, skipping." in out, (
                "expected setup to SKIP the install (mount already "
                "populated); it didn't print the skip message -- either "
                "the mount wasn't populated or setup.sh's skip guard "
                "regressed.\n" + out
            )

            # WS2's container is up; read ITS own per-workspace .bashrc
            # (a different ws_id from WS).
            ws2_id = self._await_container(name=WS2)
            bashrc_path = _owning_bashrc(self._data_dir, self._user_id, ws2_id)
            with open(bashrc_path) as f:
                bashrc = f.read()

            assert 'export OPENCLAW_HOME="/openclaw"' in bashrc, (
                "OPENCLAW_HOME missing from the second workspace's "
                "~/.bashrc even though setup completed -- a regression "
                "that moved the export inside the install-skip guard "
                "leaves it out permanently here (the install is skipped, "
                "so the guard body never runs). This is the #1039 "
                "shared-mount failure mode.\n.bashrc:\n" + bashrc
            )
            assert 'export PATH="/openclaw/bin:$PATH"' in bashrc
        finally:
            if sandbox_proc.poll() is None:
                sandbox_proc.kill()

    def test_visitor_terminal_start_mid_setup_has_openclaw_home(self):
        """A VISITOR terminal_start fired while setup is still running
        spawns a shell whose live environment has OPENCLAW_HOME set.

        This is the actual #1033 race end-to-end: a second WebSocket
        connection (not the backend's own setup connection) sends
        ``terminal_start`` while ``setup.sh`` is parked at the sentinel.
        The backend spawns the per-user tmux base session and the
        ``default-cmd`` window; the spawned shell sources ``~/.bashrc``
        at that instant. Under the #1039 fix the exports are already in
        ``~/.bashrc`` (written up front), so the spawned shell sees
        ``OPENCLAW_HOME``. Under the original bug (export appended after
        the slow install) the mid-setup shell inherits an incomplete
        ``~/.bashrc`` and ``OPENCLAW_HOME`` is empty -- exactly the
        "Missing config" window.

        The visitor's own pane (window 0) is probed because it sources
        the same ``~/.bashrc`` at the same instant as ``default-cmd``
        and sits at a clean prompt (the gateway owns default-cmd's
        foreground). We also assert the ``default-cmd`` window was
        actually spawned -- i.e. the #1033 terminal_start really fired.
        """
        # WS3 reuses the populated mount, so its setup SKIPS the install
        # (fast) and pauses at the sentinel -- setup is genuinely
        # mid-flight when the visitor connects.
        if os.path.exists(SENTINEL):
            os.remove(SENTINEL)
        with open(SENTINEL, "w") as f:
            f.write("1\n")

        sandbox_proc = subprocess.Popen(
            ["klangkc", "sandbox", WS3, SANDBOX_DIR],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            ws3_id = self._await_container(name=WS3)
            # setup.sh is now parked at the sentinel, AFTER writing the
            # consolidated .bashrc exports. A visitor connecting now
            # reproduces #1033: its terminal_start spawns a shell
            # mid-setup. Give setup a beat to reach the sentinel.
            self._await_setup_exports(ws3_id)

            value, windows = asyncio.run(
                _visitor_terminal_env(
                    self._base_url, self._token, ws3_id, "OPENCLAW_HOME"
                )
            )

            # The #1033 spawn really happened: the backend created the
            # default-cmd window for this visitor's terminal_start.
            win_names = [w.get("name") for w in windows]
            assert "default-cmd" in win_names, (
                "visitor terminal_start did not spawn the default-cmd "
                "window; windows seen: " + repr(win_names)
            )

            # The shell spawned mid-setup via the visitor path sourced a
            # ~/.bashrc that already has OPENCLAW_HOME (the #1039 fix).
            assert value == "/openclaw", (
                "a shell spawned by a visitor terminal_start mid-setup "
                "did NOT have OPENCLAW_HOME set -- the #1033 race bit: "
                "the mid-setup .bashrc lacked the export, so the spawned "
                f"shell's $OPENCLAW_HOME was {value!r} (expected "
                "'/openclaw'). This is the 'Missing config' window.\n"
                f"windows: {win_names!r}"
            )

            # Release setup.sh and let it finish.
            os.remove(SENTINEL)
            assert sandbox_proc.wait(timeout=SETUP_TIMEOUT) == 0, (
                "klangkc sandbox (WS3) failed:\n" + (sandbox_proc.stdout.read() or "")
            )
        finally:
            if os.path.exists(SENTINEL):
                os.remove(SENTINEL)
            if sandbox_proc.poll() is None:
                sandbox_proc.kill()

    def _await_container(self, name=WS, timeout=180):
        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                ws_id = _workspace_id(self._base_url, self._token, name)
                if _container_up(self._base_url, self._token, ws_id):
                    return ws_id
            except (LookupError, httpx.HTTPError) as e:
                last_err = e
            time.sleep(1)
        raise AssertionError(
            f"workspace {name} container never came up within {timeout}s\n"
            f"last error: {last_err!r}\n"
            f"--- server.log tail ---\n{self._server_log_tail()}"
        )

    def _await_setup_exports(self, ws_id, timeout=120):
        """Poll ~/.bashrc until setup has appended its first export.

        setup.sh writes the consolidated export block then blocks on the
        sentinel, so once ``export NVM_DIR`` appears setup is parked.
        Returns the path to the owning user's .bashrc.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = _owning_bashrc(self._data_dir, self._user_id, ws_id)
            if path and os.path.exists(path):
                with open(path) as f:
                    if "export NVM_DIR" in f.read():
                        return path
            time.sleep(1)
        raise AssertionError(
            "setup never wrote .bashrc exports (sentinel not reached); "
            f"user_id={self._user_id} data_dir={self._data_dir}"
        )
