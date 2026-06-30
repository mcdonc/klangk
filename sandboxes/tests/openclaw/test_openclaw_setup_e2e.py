"""E2E test: openclaw sandbox writes all ~/.profile exports before slow setup steps.

Guards #1039 (ordering) and #1087 (location).
``sandboxes/openclaw/setup.sh`` persists every env export the
default_command depends on (``NVM_DIR`` + nvm source, ``/openclaw/bin``
on ``PATH``, ``OPENCLAW_HOME``) to ``~/.profile`` in one block at the
very top of setup, before the long ``npm install -g openclaw``.

Why ``~/.profile`` and not ``~/.bashrc`` (#1087): ``~/.profile`` is the
POSIX file sourced by ALL login shells -- interactive terminals (the
default-cmd tmux pane is an interactive login shell) AND non-interactive
``bash -lc`` (which the health check uses, ``container.py``
``HealthMonitor._run_one``). ``~/.bashrc`` has an interactivity guard
that hides its body from non-interactive shells, so exports the health
check needs cannot live there. ``~/.profile`` is the one file BOTH the
default_command and the health check reliably source.

Previously ``OPENCLAW_HOME`` was appended near the end of setup (and to
``~/.bashrc``), so a shell spawned mid-setup (e.g. the ``default-cmd``
pane from an early ``terminal_start`` -- the #1033 race) inherited
``PATH`` but not ``OPENCLAW_HOME``; with ``OPENCLAW_HOME`` unset,
``openclaw gateway`` looked for config at ``$HOME/.openclaw`` instead of
``/openclaw/.openclaw`` and reported "Missing config".

How the test exercises this deterministically: ``setup.sh`` blocks while
a sentinel file (``/openclaw/.klangk-test-pause``, i.e. on the bind
mount) exists. The sentinel is placed right after the consolidated export
block and before the slow install, so while setup is parked there the
test reads ``~/.profile`` straight off the host filesystem and asserts
the export is already present. On a regression that moves the
``OPENCLAW_HOME`` append back to the end of setup, ``~/.profile`` lacks it
at this point and the test fails. Releasing the sentinel lets the real
``npm install -g openclaw`` run, and the test confirms the binary lands
on the mount.

This reads files directly from the host (the per-user home and the
``/openclaw`` mount both live under the server data dir / the sandbox
dir). It deliberately avoids ``klangkc exec``: exec runs a raw command
with no login shell (#1041), so it would not source ``~/.profile`` and
would give a false negative.

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
# install but must still write a complete per-workspace ~/.profile. This
# is the shared-mount + per-workspace-~/.profile interaction at the heart
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


def _owning_profile(data_dir, user_id, ws_id):
    """Path to the owning user's ~/.profile on the host for *ws_id*.

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
        ".profile",
    )


async def _visitor_two_phase_terminal_env(
    base_url, token, ws_id, var, between_phases, timeout=25.0
):
    """Two-phase visitor probe for the #1033/#1051/#1039 invariants.

    Phase 1 -- mid-setup: fire ``terminal_start`` while setup is parked
    at the sentinel.  Only the ``bash`` window (window 0) is created;
    ``default-cmd`` is gated on ``setup_state == complete`` (#1051).
    The spawned shell sources ``~/.profile`` at that instant; under the
    #1039 fix the exports are already present, so ``$var`` is set.

    Then ``await between_phases()`` runs -- the caller releases the
    sentinel and waits for setup to finish (the setup connection's own
    post-setup ``terminal_start`` creates the ``default-cmd`` window).

    Phase 2 -- post-setup: fire ``terminal_start`` again.  The
    ``default-cmd`` window now exists; the visitor observes it via the
    window sync.  (The visitor does not itself receive
    ``default_command_started`` here -- the setup connection won the
    race to create the window.  That event is covered by unit tests.)

    Returns ``(mid_windows, post_windows, value)``.
    """
    ws_url = base_url.replace("http://", "ws://") + "/ws"
    async with websockets.connect(f"{ws_url}?token={token}", max_size=2**24) as ws:
        await ws.send(json.dumps({"cmd": "workspace_connect", "workspaceId": ws_id}))
        # Drain until container_ready.
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("visitor: no container_ready")
            msg = json.loads(await asyncio.wait_for(ws.recv(), remaining))
            if msg.get("type") == "container_ready":
                break
            if msg.get("type") == "error":
                raise ConnectionError(f"visitor connect failed: {msg}")

        # ---- Phase 1: terminal_start mid-setup ----
        mid_windows = await _visitor_fire_terminal_start(ws, timeout)
        value = await _visitor_probe_env(ws, var, timeout)

        # Let setup finish (caller releases sentinel + waits).
        await between_phases()

        # ---- Phase 2: terminal_start post-setup ----
        post_windows = await _visitor_fire_terminal_start(ws, timeout)

        return mid_windows, post_windows, value


async def _visitor_fire_terminal_start(ws, timeout):
    """Send terminal_start, drain to terminal_windows, return windows."""
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
    deadline = asyncio.get_event_loop().time() + timeout
    windows = []
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
    return windows


async def _visitor_probe_env(ws, var, timeout):
    """Probe window 0's live ``$var`` via a marker, retrying until ready."""
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
            sent = False
            continue
        msg = json.loads(raw)
        if msg.get("type") == "terminal_output":
            buf += msg.get("data", "")
            for m in pattern.finditer(buf):
                val = m.group(1)
                if val != "%s":
                    return val
    raise AssertionError(
        f"visitor probe for ${var} never returned a marker within "
        f"{timeout}s. Output:\n{buf!r}"
    )


class TestOpenclawSetupProfileExports:
    """Real openclaw sandbox: ~/.profile exports precede the slow install."""

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

    def test_profile_has_openclaw_home_before_slow_install(self):
        """~/.profile contains OPENCLAW_HOME before the npm install runs.

        This is the #1039 invariant: every export the default_command
        depends on is written up front, so a shell spawned at any point
        during setup (here, held by the sentinel after the export block
        but before the npm install) sources a complete ~/.profile. Under
        the original bug OPENCLAW_HOME was appended after the install, so
        .profile would be missing it at this point.
        """
        env = self._env

        # Run the sandbox in the background; it blocks on the sentinel
        # right after writing the consolidated ~/.profile exports.
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
            profile_path = self._await_setup_exports(ws_id)
            with open(profile_path) as f:
                profile = f.read()

            # The invariant under test: OPENCLAW_HOME is already in
            # ~/.profile while setup is still parked before the slow
            # install.
            assert 'export OPENCLAW_HOME="/openclaw"' in profile, (
                "OPENCLAW_HOME missing from ~/.profile before the slow "
                "install step -- the #1039 export ordering regressed; a "
                "shell spawned during setup cannot locate openclaw "
                "config.\n~/.profile:\n" + profile
            )
            assert 'export PATH="/openclaw/bin:$PATH"' in profile

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

    def test_second_workspace_reuses_install_but_writes_own_profile(self):
        """A second workspace at the same /openclaw mount SKIPS the install
        (openclaw is already there) yet must still write a complete
        per-workspace ~/.profile.

        This is the shared-mount + per-workspace-~/.profile interaction
        at the heart of #1039. ``~/.profile`` is fresh per workspace (the
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
            # the full install and wrote ~/.profile that way.
            assert "openclaw already installed, skipping." in out, (
                "expected setup to SKIP the install (mount already "
                "populated); it didn't print the skip message -- either "
                "the mount wasn't populated or setup.sh's skip guard "
                "regressed.\n" + out
            )

            # WS2's container is up; read ITS own per-workspace ~/.profile
            # (a different ws_id from WS).
            ws2_id = self._await_container(name=WS2)
            profile_path = _owning_profile(self._data_dir, self._user_id, ws2_id)
            with open(profile_path) as f:
                profile = f.read()

            assert 'export OPENCLAW_HOME="/openclaw"' in profile, (
                "OPENCLAW_HOME missing from the second workspace's "
                "~/.profile even though setup completed -- a regression "
                "that moved the export inside the install-skip guard "
                "leaves it out permanently here (the install is skipped, "
                "so the guard body never runs). This is the #1039 "
                "shared-mount failure mode.\n~/.profile:\n" + profile
            )
            assert 'export PATH="/openclaw/bin:$PATH"' in profile
        finally:
            if sandbox_proc.poll() is None:
                sandbox_proc.kill()

    def test_visitor_terminal_start_mid_setup_has_openclaw_home(self):
        """A VISITOR terminal_start fired while setup is still running
        spawns a shell whose live environment has OPENCLAW_HOME set.

        Two-phase assertion reflecting #1051's gating of the default
        command on ``setup_state == complete``:

        Phase 1 (mid-setup): the visitor's ``terminal_start`` creates
        the ``bash`` window (window 0) but NOT ``default-cmd`` -- #1051
        gates it.  The spawned shell sources ``~/.profile`` at that
        instant; under the #1039 fix the exports are already there
        (written before the slow install), so ``OPENCLAW_HOME`` is set.
        This is the "Missing config" window that #1039 closes.

        Phase 2 (post-setup): after setup completes, the ``default-cmd``
        window appears (created by the setup connection's own
        post-setup ``terminal_start``).  The visitor observes it via a
        window sync on its second ``terminal_start``.
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
            # consolidated ~/.profile exports.
            self._await_setup_exports(ws3_id)

            async def _release_and_wait_setup():
                """Release sentinel, wait for sandbox proc to finish."""

                def _wait():
                    os.remove(SENTINEL)
                    deadline = time.monotonic() + SETUP_TIMEOUT
                    while time.monotonic() < deadline:
                        if sandbox_proc.poll() is not None:
                            return sandbox_proc.returncode
                        time.sleep(0.5)
                    return None

                rc = await asyncio.to_thread(_wait)
                assert rc == 0, "klangkc sandbox (WS3) failed:\n" + (
                    sandbox_proc.stdout.read() or ""
                )

            mid_windows, post_windows, value = asyncio.run(
                _visitor_two_phase_terminal_env(
                    self._base_url,
                    self._token,
                    ws3_id,
                    "OPENCLAW_HOME",
                    _release_and_wait_setup,
                )
            )

            # Phase 1 -- mid-setup: default-cmd is gated (#1051).
            mid_names = [w.get("name") for w in mid_windows]
            assert "default-cmd" not in mid_names, (
                "visitor terminal_start mid-setup created the "
                "default-cmd window, but #1051 should gate it on "
                "setup_state == complete. Windows seen: " + repr(mid_names)
            )

            # The shell spawned mid-setup sourced a ~/.profile that
            # already has OPENCLAW_HOME (the #1039 fix).
            assert value == "/openclaw", (
                "a shell spawned by a visitor terminal_start mid-setup "
                "did NOT have OPENCLAW_HOME set -- the #1033 race bit: "
                "the mid-setup ~/.profile lacked the export, so the spawned "
                f"shell's $OPENCLAW_HOME was {value!r} (expected "
                "'/openclaw'). This is the 'Missing config' window."
            )

            # Phase 2 -- post-setup: default-cmd now exists (created by
            # the setup connection's own post-setup terminal_start).
            post_names = [w.get("name") for w in post_windows]
            assert "default-cmd" in post_names, (
                "default-cmd window absent post-setup; the #1051 gate "
                "should open once setup_state == complete. Windows seen: "
                + repr(post_names)
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
        """Poll ~/.profile until setup has appended its first export.

        setup.sh writes the consolidated export block then blocks on the
        sentinel, so once ``export NVM_DIR`` appears setup is parked.
        Returns the path to the owning user's .profile.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = _owning_profile(self._data_dir, self._user_id, ws_id)
            if path and os.path.exists(path):
                with open(path) as f:
                    if "export NVM_DIR" in f.read():
                        return path
            time.sleep(1)
        raise AssertionError(
            "setup never wrote ~/.profile exports (sentinel not reached); "
            f"user_id={self._user_id} data_dir={self._data_dir}"
        )
