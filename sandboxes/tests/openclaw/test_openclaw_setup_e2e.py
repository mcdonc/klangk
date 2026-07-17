"""E2E tests for the openclaw sandbox.

Covers three invariants:

- #1039 (ordering): ``setup.sh`` writes every env export the
  service_command depends on into the AGENT's ``~/.profile`` up front,
  before the slow ``npm install``.
- #1039 (shared mount): a second workspace reusing the populated
  ``/openclaw`` mount SKIPS the install yet still writes a complete
  agent ``~/.profile``.
- #1089: the ``health-check: /openclaw/bin/healthcheck.sh`` config
  reaches the workspace, the health monitor runs it as a non-login
  bash shell (``bash -c``) so it sources nothing, and the status
  endpoint reports ``healthy`` once the gateway (launched by
  ``service-command``) is up.

Under the agent-owns-the-service model (#1133/#1158) the service command
runs in the agent's standalone ``service`` tmux session, whose login shell
sources the AGENT's ``~/.profile``. So ``setup.sh`` repoints ``HOME`` at
``$KLANGK_AGENT_HOME`` and writes its exports there (``NVM_DIR`` + nvm
source, ``/openclaw/bin`` on ``PATH``, ``OPENCLAW_HOME``); the owner
manages openclaw through the Service terminal tab, not their own shell, so
nothing openclaw-related lives in the owner's home (#1171). The health
check is unaffected: it runs as a non-login ``bash -c`` and uses an
absolute-path wrapper that bakes in ``OPENCLAW_HOME``.

Why ``~/.profile`` and not ``~/.bashrc`` (#1087): ``~/.profile`` is the
POSIX file sourced by login shells. ``~/.bashrc`` has an interactivity
guard that hides its body from non-interactive shells.

Previously the exports went to the OWNER's ``~/.profile`` (pre-#1158 the
service command ran in the owner's session); that left the autostarted
gateway env-less under the new model -- the service session sourced the
agent's empty ``~/.profile`` and ``openclaw gateway`` reported "Missing
config" (#1171).

How the test exercises ordering deterministically: ``setup.sh`` blocks
while a sentinel file (``/openclaw/.klangk-test-pause``, i.e. on the bind
mount) exists. The sentinel is placed right after the consolidated export
block and before the slow install, so while setup is parked there the test
reads the agent's ``~/.profile`` straight off the host filesystem and
asserts the export is already present. Releasing the sentinel lets the
real ``npm install -g openclaw`` run, and the test confirms the binary
lands on the mount.

This reads files directly from the host (the per-user home and the
``/openclaw`` mount both live under the server data dir / the sandbox
dir).

Run locally (the workspace image must be built first):

    devenv shell -- klangk:build-workspace-image
    devenv shell -- pytest sandboxes/tests/openclaw -v -p no:xdist --no-cov

Gated to its own workflow (sandbox-openclaw-e2e) on changes to the
sandbox, so it never slows the regular CLI/backend e2e suites. Requires
network (real ``npm install``).
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

# Launch the real ``klangkd`` via the shared backend E2E launcher (#1525).
# ``runtestserver.py`` was retired when every suite migrated off the
# test-only uvicorn shim to the production entry point; this standalone
# suite imports that launcher instead of reimplementing subprocess wiring.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "src",
        "klangk",
        "klangkd-tests",
        "e2e-tests",
    ),
)
from _e2e_server import start_server, stop_server

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SANDBOX_DIR = os.path.join(REPO_ROOT, "sandboxes", "openclaw")
# setup.sh blocks while this file exists on the mount (/openclaw is the
# bind-mounted sandbox dir).
SENTINEL = os.path.join(SANDBOX_DIR, ".klangk-test-pause")

WS = "e2e-openclaw-setup"
# Second workspace pointed at the SAME /openclaw mount. openclaw is
# already installed there after WS's setup ran, so WS2's setup SKIPS the
# install but must still write a complete agent ~/.profile. This is the
# shared-mount + per-workspace-~/.profile interaction at the heart of
# #1039 -- a regression that moves an export inside the install-skip
# guard breaks WS2 permanently (not just a race) while WS may still pass.
WS2 = "e2e-openclaw-setup-2"
# Third workspace at the same mount, used by the #1089 health-check test.
# Like WS2 its setup skips the install (mount already populated) so it
# comes up fast; the point is to exercise the end-to-end health-check
# path (config -> monitor -> status endpoint) against a real gateway, not
# to re-run the install.
WS3 = "e2e-openclaw-setup-3"
EMAIL = "test@example.com"
PASSWORD = "testpass"
# The agent's user id (klangk.model.AGENT_USER_ID). setup.sh
# repoints HOME at the agent's home (#1171) so the ~/.profile exports
# land in the agent's home, not the owner's; the tests read that profile.
AGENT_USER_ID = "00000000-0000-0000-0000-000000000001"
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
        json={"identifier": EMAIL, "password": PASSWORD},
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


def _start_server(data_dir, extra_env=None):
    """Start a real ``klangkd`` (nginx on a TCP port); return (handle, url).

    ``uds=False`` gives this suite a real ``http://localhost:<port>`` URL —
    nginx fronts klangkd's UDS (#1525) — which the CLI (``klangk login``)
    and the suite's ``httpx`` calls need. ``clean_env`` strips every
    ``KLANGK_*`` by default (#1526), so the runner-provided infra the
    openclaw gateway depends on — system podman (nix podman lacks SUID
    newuidmap on Ubuntu runners) and the LLM endpoint setup.sh bakes into
    the gateway config — is forwarded explicitly from the ambient env.
    """
    log_path = os.path.join(data_dir, "server.log")
    overrides = {
        "KLANGK_JWT_SECRET": "openclaw-e2e-test-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": EMAIL,
        "KLANGK_DEFAULT_PASSWORD": PASSWORD,
        "KLANGK_AUTH_MODES": "password",  # these tests use password login
        "KLANGK_TEST_MODE": "1",
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        # Poll health every 3s instead of the 30s default so the
        # #1089 health-check test sees the healthy transition quickly
        # (the gateway takes a few seconds to bind after setup). This
        # is harmless to the other tests: they hold setup_state=pending
        # at the sentinel (health checks are skipped until complete) or
        # have already finished asserting by the time polling starts.
        "KLANGK_HEALTH_CHECK_INTERVAL": "3",
        "KLANGK_ALLOW_AUTOSTART": "1",
        "LOGFIRE_TOKEN": "",
        "log_path": log_path,
    }
    # Runner/devenv infra clean_env would otherwise strip (#1526): system
    # podman + the LLM endpoint the openclaw gateway proxies to.
    for _k in (
        "KLANGK_PODMAN_BIN",
        "KLANGK_LLM_API_KEY",
        "KLANGK_LLM_BASE_URL",
        "KLANGK_LLM_MODEL",
    ):
        _v = os.environ.get(_k)
        if _v is not None:
            overrides[_k] = _v
    if extra_env:
        overrides.update(extra_env)
    server = start_server(uds=False, data_dir=data_dir, **overrides)
    return server, server["url"]


def _stop_server(server, data_dir=None):
    """Stop a server started by ``_start_server``.

    ``stop_server`` kills the klangkd subprocess, removes its
    instance-labelled containers, and deletes the data/state dirs — the
    per-instance sweep this suite used to inline (#1393, #1553).
    """
    stop_server(server)


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


def _health_status(base_url, token, ws_id):
    """Return (health, health_checked_at) from the status endpoint.

    ``health`` is ``None`` until the first check completes (or when no
    health_check is configured), then ``"healthy"`` / ``"unhealthy"``.
    """
    r = httpx.get(
        f"{base_url}/api/v1/workspaces/{ws_id}/status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("health"), body.get("health_checked_at")


def _agent_profile(data_dir, ws_id):
    """Path to the AGENT's ~/.profile on the host for *ws_id*.

    setup.sh repoints HOME at the agent's home (#1171), so the
    OPENCLAW_HOME / PATH / NVM_DIR exports it writes land in the agent's
    home (``.users/<AGENT_USER_ID>``), not the owner's. Since #1295
    workspace storage is keyed by workspace_id, so the home tree
    is ``<data>/workspaces/<ws_id>/home/.users/``.
    """
    return os.path.join(
        data_dir,
        "workspaces",
        ws_id,
        "home",
        ".users",
        AGENT_USER_ID,
        ".profile",
    )


class TestOpenclawSetupProfileExports:
    """Real openclaw sandbox: ~/.profile exports precede the slow install."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def server(tmp_path_factory, request):
        data_dir = tempfile.mkdtemp(prefix="klangk-openclaw-e2e-")
        server, base_url = _start_server(data_dir)
        config_dir = tmp_path_factory.mktemp("klangk-openclaw-config")
        env = {**os.environ, "HOME": str(config_dir)}
        os.makedirs(config_dir / ".config" / "klangk", exist_ok=True)
        _run(
            ["klangk", "login", base_url, EMAIL, "--password-file", "-"],
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
        _stop_server(server, data_dir)

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

        This is the #1039 invariant: every export the service_command
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
            ["klangk", "sandbox", WS, SANDBOX_DIR],
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
                "klangk sandbox failed:\n" + (sandbox_proc.stdout.read() or "")
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
                    print(f"--- klangk sandbox output ---\n{out}")

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
            ["klangk", "sandbox", WS2, SANDBOX_DIR],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            rc = sandbox_proc.wait(timeout=SETUP_TIMEOUT)
            out = sandbox_proc.stdout.read() or ""
            assert rc == 0, f"klangk sandbox (WS2) failed:\n{out}"

            # The install was genuinely skipped -- proving we're on the
            # skip path, not a vacuous pass where a regression re-ran
            # the full install and wrote ~/.profile that way.
            assert "openclaw already installed, skipping." in out, (
                "expected setup to SKIP the install (mount already "
                "populated); it didn't print the skip message -- either "
                "the mount wasn't populated or setup.sh's skip guard "
                "regressed.\n" + out
            )

            # WS2's container is up; read ITS agent ~/.profile
            # (a different ws_id from WS).
            ws2_id = self._await_container(name=WS2)
            profile_path = _agent_profile(self._data_dir, ws2_id)
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

    def test_health_check_reports_healthy_when_gateway_up(self):
        """The ``health-check: /openclaw/bin/healthcheck.sh`` config reaches
        the workspace, the health monitor runs it as a non-login bash
        shell (``bash -c``) so it sources nothing, and the status
        endpoint reports ``healthy`` once the gateway (launched by
        ``service-command``) is up.

        This is the #1089 end-to-end validation. The whole chain must
        work for the status to flip to healthy:

        1. ``klangk sandbox`` creates the workspace carrying
           ``health_check`` from the sandbox config.
        2. setup completes -> ``setup_state == complete`` (the monitor
           skips checks until then).
        3. ``service-command: openclaw gateway`` fires and the gateway
           binds its port.
        4. the monitor runs ``bash -c /openclaw/bin/healthcheck.sh``;
           the non-login shell sources nothing, but the wrapper script
           sets ``OPENCLAW_HOME`` and execs the symlinked
           ``/openclaw/bin/openclaw health`` by absolute path, which
           connects to the gateway over WebSocket and exits 0.

        The gateway takes a few seconds to bind after setup, so an
        initial ``unhealthy`` window is expected and tolerated -- the
        test waits for the transition to ``healthy``.
        """
        # Precondition: openclaw on the shared mount (full-install test
        # ran first). Fail fast rather than doing a misleading full
        # install that would hide a config-wiring regression.
        assert glob.glob(
            os.path.join(
                SANDBOX_DIR, ".nvm", "versions", "node", "v*", "bin", "openclaw"
            )
        ), (
            "openclaw not on the shared mount -- run the full-install test "
            "first so this test exercises the health-check path, not a "
            "full install"
        )

        # WS3 reuses the populated mount, so its setup SKIPS the install
        # (fast) -- the point here is the health-check path, not install.
        sandbox_proc = subprocess.Popen(
            ["klangk", "sandbox", WS3, SANDBOX_DIR],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            rc = sandbox_proc.wait(timeout=SETUP_TIMEOUT)
            out = sandbox_proc.stdout.read() or ""
            assert rc == 0, f"klangk sandbox (WS3) failed:\n{out}"
            # The install was genuinely skipped, proving the fast path.
            assert "openclaw already installed, skipping." in out, (
                "expected setup to SKIP the install (mount already "
                "populated); it didn't.\n" + out
            )

            ws3_id = self._await_container(name=WS3)

            # Wait for the gateway to come up AND the monitor (polling
            # every KLANGK_HEALTH_CHECK_INTERVAL=3s in this fixture) to
            # report healthy. Generous timeout: the gateway binds a few
            # seconds after setup, then the first poll lands within one
            # interval.
            self._await_health(ws3_id, expected="healthy", timeout=180)
        finally:
            if sandbox_proc.poll() is None:
                sandbox_proc.kill()

    def _await_health(self, ws_id, expected="healthy", timeout=180):
        """Poll /status until ``health == expected`` or timeout.

        Raises with diagnostics on timeout -- the last status and the
        server log tail -- so a failure distinguishes "never checked"
        (health stuck at None -> config-wiring bug) from "always
        unhealthy" (check ran but the gateway never came up).
        """
        deadline = time.monotonic() + timeout
        last = None
        last_checked_at = None
        while time.monotonic() < deadline:
            last, last_checked_at = _health_status(self._base_url, self._token, ws_id)
            if last == expected:
                return
            time.sleep(2)
        raise AssertionError(
            f"workspace health never reached {expected!r} within "
            f"{timeout}s (last={last!r}, "
            f"health_checked_at={last_checked_at!r}). If last is None "
            "the health check never ran (config-wiring bug: "
            "health_check did not reach the workspace, or "
            "setup_state never became complete). If last is "
            "'unhealthy' the check ran but the gateway never came up "
            "or the command could not resolve.\n"
            f"--- server.log tail ---\n{self._server_log_tail()}"
        )

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
        """Poll the agent's ~/.profile until setup has written ALL exports.

        setup.sh writes NVM_DIR, then PATH, then OPENCLAW_HOME, then blocks
        on the sentinel. Waiting for OPENCLAW_HOME (the last export)
        guarantees setup is parked at the sentinel with the full export set
        written — waiting on just NVM_DIR (the first) races on slow runners,
        which can read the file between the PATH and OPENCLAW_HOME appends.
        Returns the path to the agent's .profile.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = _agent_profile(self._data_dir, ws_id)
            if path and os.path.exists(path):
                with open(path) as f:
                    if "export OPENCLAW_HOME" in f.read():
                        return path
            time.sleep(1)
        # Timed out — dump whatever partial profile exists so we can see
        # exactly where setup stopped (which exports landed, which didn't).
        path = _agent_profile(self._data_dir, ws_id)
        partial = ""
        if path and os.path.exists(path):
            with open(path) as f:
                partial = f.read()
        raise AssertionError(
            "setup never wrote the agent's ~/.profile exports "
            "(sentinel not reached); "
            f"user_id={self._user_id} data_dir={self._data_dir}\n"
            f"--- partial ~/.profile ---\n{partial}"
        )
