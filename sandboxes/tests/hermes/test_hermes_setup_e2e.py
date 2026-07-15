"""E2E tests for the hermes sandbox.

Covers two invariants (#1109):

- **Installs as a sandbox.** ``klangkc sandbox hermes`` fetches and runs the
  upstream Hermes installer at runtime (per-workspace, as the non-root
  ``klangk`` user), writes ``~/.profile`` exports + the llm-proxy config, and
  lands the ``hermes`` binary.
- **Health-check works.** The
  ``health-check: /hermes/bin/healthcheck.sh`` config reaches the
  workspace, the health monitor runs it as a non-login bash shell
  (``bash -c``) so it sources nothing, and the status endpoint reports
  ``healthy`` once the gateway (launched by ``service-command``) is up.
  ``setup.sh`` writes the wrapper to ``/hermes/bin/healthcheck.sh``; it
  sets ``HERMES_HOME`` and calls the venv binary by absolute path,
  grepping its output for the running marker.

  ``hermes gateway status`` always exits 0 (it only prints state), so the
  liveness signal is derived from its printed output -- hermes's own process
  detection (PID file + ``/proc`` scan) does the work. With no messaging
  platforms configured the gateway idles for cron job execution rather than
  exiting, so it reports healthy even without Telegram/Discord tokens.

Hermes was previously a compile-time plugin; converting it to a runtime
sandbox is what made the ``/tmp/.klangk-image-build`` bailout in
``bash.bashrc`` dead code (the installer's ``bash -i`` PATH probe only runs
in the root/FHS branch, which a non-root sandbox never takes).

This reads files directly from the host (the per-user home and the ``/hermes``
mount both live under the server data dir / the sandbox dir). It deliberately
avoids ``klangkc exec`` for the profile check: exec runs a raw command with
no login shell (#1041), so it would not source ``~/.profile`` and would give
a false negative.

Run locally (the workspace image must be built first):

    devenv shell -- klangk:build-workspace-image
    devenv shell -- pytest sandboxes/tests/hermes -v -p no:xdist --no-cov

Gated to the sandbox-e2e workflow (runs on changes to ``sandboxes/**``),
alongside the openclaw suite. Requires network (real ``git clone`` + ``uv
sync`` of the upstream repo).
"""

import os
import shutil
import subprocess
import tempfile
import time

import httpx
import pytest

from klangk_backend.model import free_port

from pathlib import Path

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SANDBOX_DIR = os.path.join(REPO_ROOT, "sandboxes", "hermes")

WS = "e2e-hermes-setup"
# Free port (allocated once at import) so this never collides with other
# e2e suites or concurrent runs (#1393).
PORT = str(free_port())
EMAIL = "test@example.com"
PASSWORD = "testpass"
# The agent's user id (klangk_backend.model.AGENT_USER_ID). setup.sh
# repoints HOME at the agent's home (#1171) so the ~/.profile exports
# land in the agent's home, not the owner's; the test reads that profile.
AGENT_USER_ID = "00000000-0000-0000-0000-000000000001"
# Real install (clone NousResearch/hermes-agent + uv sync .[all] + managed
# Node) can take several minutes, especially on CI. The installer's own
# estimate is 1-5 min for deps; the clone + Node add to that.
SETUP_TIMEOUT = 1200


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
    headers = {"Authorization": f"Bearer {token}"}
    mp = httpx.get(f"{base_url}/api/v1/my-permissions", headers=headers, timeout=30)
    mp.raise_for_status()
    user_id = mp.json()["user_id"]
    return token, user_id


def _start_server(data_dir, port, extra_env=None):
    env = {
        **os.environ,
        "_KLANGK_DISABLE_NGINX": "1",  # bare uvicorn; lifespan must not spawn nginx
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "hermes-e2e-test-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": EMAIL,
        "KLANGK_DEFAULT_PASSWORD": PASSWORD,
        "KLANGK_AUTH_MODES": "password",  # these tests use password login
        "KLANGK_TEST_MODE": "1",
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        # Poll health every 3s so the health-check test sees the healthy
        # transition quickly (the gateway takes a few seconds to start after
        # setup). Health checks are skipped until setup_state == complete, so
        # this is harmless during the install.
        "KLANGK_HEALTH_CHECK_INTERVAL": "3",
        "KLANGK_PORT_RANGE_START": str(free_port()),
        "KLANGK_ALLOW_AUTOSTART": "1",
        "LOGFIRE_TOKEN": "",
        **(extra_env or {}),
    }
    log_path = os.path.join(data_dir, "server.log")
    log_file = open(log_path, "w")  # noqa: SIM115
    # Launch via runtestserver.py (build_app() explicitly) — the composition
    # root is sealed (#1454), so there's no module-level ``app`` for
    # ``uvicorn klangk_backend.main:app`` to import.
    proc = subprocess.Popen(
        [
            "python3",
            os.path.join(REPO_ROOT, "src", "backend", "e2e-tests", "runtestserver.py"),
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--ws-max-size",
            "16777216",
            "--ws-ping-interval",
            "20",
            "--ws-ping-timeout",
            "20",
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


def _stop_server(proc, data_dir):
    if hasattr(proc, "_log_file"):
        proc._log_file.close()
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
    # Instance-scoped cleanup: only remove containers THIS test server
    # started (label=klangk.instance=<id>), never another suite's or xdist
    # worker's. The old ``label=klangk.managed=true`` filter was a cross-run
    # hazard once suites could run concurrently (#1393). The ID lives in
    # ``<data_dir>/instance-id" (written by klangkd at startup, #1553); read
    # it directly rather than shelling out to a console script (#1565).
    _id_file = Path(data_dir) / "instance-id"
    instance_id = _id_file.read_text().strip() if _id_file.exists() else ""
    if instance_id:
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
    """SIGKILL any process bound to *port* (TERM is not always enough)."""
    try:
        out = subprocess.run(["ss", "-tlnHp"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return
    for line in out.splitlines():
        if f":{port}" not in line:
            continue
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


def _health_status(base_url, token, ws_id):
    """Return (health, health_checked_at) from the status endpoint.

    ``health`` is ``None`` until the first check completes, then
    ``"healthy"`` / ``"unhealthy"``.
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
    HERMES_HOME / PATH exports it writes land in the agent's home
    (``.users/<AGENT_USER_ID>``), not the owner's. Since #1295
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


class TestHermesSetup:
    """Real hermes sandbox: install completes, gateway health-check is healthy."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def server(tmp_path_factory, request):
        data_dir = tempfile.mkdtemp(prefix="klangk-hermes-e2e-")
        proc, base_url = _start_server(data_dir, PORT)
        config_dir = tmp_path_factory.mktemp("klangk-hermes-config")
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
        # Clean the shared /hermes mount so every run is deterministic.
        # These are gitignored install artifacts, never committed.
        for art in (
            "hermes-agent",
            "node",
            "bin",
            ".env",
            "config.yaml",
            "gateway.pid",
            "gateway.lock",
            "gateway_state.json",
            "logs",
        ):
            p = os.path.join(SANDBOX_DIR, art)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        yield
        _stop_server(proc, data_dir)
        _force_kill_port(PORT)

    def _server_log_tail(self, n=40):
        log_path = os.path.join(self._data_dir, "server.log")
        if not os.path.exists(log_path):
            return "(no server.log)"
        with open(log_path) as f:
            return "".join(f.readlines()[-n:])

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

    def _await_health(self, ws_id, expected="healthy", timeout=240):
        """Poll /status until ``health == expected`` or timeout."""
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
            "health_check did not reach the workspace, or setup_state "
            "never became complete). If last is 'unhealthy' the check "
            "ran but the gateway never came up or the command could not "
            "resolve.\n"
            f"--- server.log tail ---\n{self._server_log_tail()}"
        )

    def test_hermes_installs_and_health_check_reports_healthy(self):
        """The hermes sandbox installs at runtime and its health-check
        reports healthy once the gateway (started by ``service-command``)
        is up.

        This is the #1109 end-to-end validation. The whole chain must work:

        1. ``klangkc sandbox hermes`` creates the workspace carrying
           ``service_command``, ``health_check``, and ``auto_start`` from
           the sandbox config.
        2. ``setup.sh`` writes ``~/.profile`` exports up front, fetches +
           runs the upstream installer (non-root, so the ``bash -i`` PATH
           probe is never taken), writes the llm-proxy config, and copies
           the gateway wrapper.
        3. setup completes -> ``service-command`` (``klangk-hermes-gateway``)
           fires; the wrapper refreshes the token then runs
           ``hermes gateway run``.
        4. the monitor runs ``bash -c /hermes/bin/healthcheck.sh``; the
           non-login shell sources nothing, but the wrapper sets
           ``HERMES_HOME`` and calls the venv ``hermes gateway status``
           by absolute path, grepping for the running marker.
           hermes's process detection finds the running gateway ->
           status endpoint reports ``healthy``.
        """
        sandbox_proc = subprocess.Popen(
            ["klangkc", "sandbox", WS, SANDBOX_DIR],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            rc = sandbox_proc.wait(timeout=SETUP_TIMEOUT)
            out = sandbox_proc.stdout.read() or ""
            assert rc == 0, "klangkc sandbox (hermes install) failed:\n" + out

            ws_id = self._await_container()

            # setup.sh ran the real installer: the repo clone + venv land on
            # the /hermes mount (== the sandbox dir on the host).
            assert os.path.isdir(os.path.join(SANDBOX_DIR, "hermes-agent")), (
                "hermes-agent repo not cloned onto the /hermes mount"
            )
            assert os.path.exists(os.path.join(SANDBOX_DIR, "config.yaml")), (
                "setup.sh did not write /hermes/config.yaml (llm-proxy config)"
            )
            # setup.sh wrote ~/.profile exports up front (into the AGENT's
            # home: it repoints HOME at $KLANGK_AGENT_HOME, #1171).
            profile_path = _agent_profile(self._data_dir, ws_id)
            assert os.path.exists(profile_path), (
                f"agent ~/.profile not found at {profile_path}"
            )
            with open(profile_path) as f:
                profile = f.read()
            assert 'export HERMES_HOME="/hermes"' in profile, (
                "HERMES_HOME missing from the agent's ~/.profile -- the "
                "gateway runs in the agent's service session which sources "
                "this file, so without it the autostarted gateway cannot "
                "locate hermes config/PID files (#1171).\n"
                "~/.profile:\n" + profile
            )

            # The gateway (started by service-command) is up and the health
            # monitor (polling every 3s in this fixture) reports healthy.
            self._await_health(ws_id, expected="healthy", timeout=240)
        finally:
            if sandbox_proc.poll() is None:
                sandbox_proc.kill()
