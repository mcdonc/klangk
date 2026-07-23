"""E2E: klangkd + the **nginx** engine under a real ``systemctl --user`` service.

Validates #1727 end-to-end — the path the unit tests can't reach because they
monkeypatch ``stdout_is_reopenable`` and never render a config that a *real*
nginx parses against a *real* journald socket fd:

- a real ``fstat(1)`` probe sees a real journald socket (systemd gives every
  user service ``StandardOutput=journal`` → fd 1 is an ``AF_UNIX`` socket),
- the renderer takes the ``syslog:server=unix:/dev/log`` branch (#1727) — not
  the legacy ``access_log /dev/stdout;`` that nginx can't re-``open()`` under
  systemd (``ENXIO`` → crash-loop, #1550),
- a real nginx parses that config, **stays up**, serves a request, and routes
  its access log to the journal over ``/dev/log``.

The existing e2e suites (``test_proxy_lifecycle_e2e.py``,
``test_proxy_acl_e2e.py``) launch nginx as a bare pytest *subprocess* whose fd 1
is a pipe → ``stdout_is_reopenable()`` returns True → the legacy ``/dev/stdout``
branch → the systemd branch is never exercised. This suite closes that gap by
running ``klangkd`` (nginx engine) as a **transient systemd user service** via
``systemd-run --user`` so fd 1 is the real journald socket.

GitHub Actions runners do not run a systemd user manager (no PID-1 systemd, no
``/dev/log``), so this suite **skips** there. Run it on a real Linux+systemd
host (the NixOS dev box) before each release, e.g.::

    devenv shell -- test-systemd-nginx

It fails if #1727's renderer change is reverted: nginx re-enters the
``/dev/stdout`` ``ENXIO`` crash-loop, the browser listener never serves (the
``test_proxy_serves_request`` assertion fails), and the ``#1550`` signature
(``open() "/dev/stdout" failed`` + ``nginx exited (rc=1)``) appears in the
journal (``test_no_dev_stdout_crash_loop_signature`` fails).
"""

import os
import re
import shutil
import subprocess
import time

import httpx
import pytest

from klangk.model import free_port
from _e2e_env import clean_env

BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")

# How long to wait for the transient service to answer /health. Container-less
# cold start (settings + DB seed + podman prewarm + nginx spawn) is a few
# seconds; the headroom absorbs a loaded host.
_READINESS_TIMEOUT = 60

# nginx's "I cannot reopen /dev/stdout under systemd" emerg line — the exact
# #1550 regression signature. Present iff the renderer wrongly took the
# ``access_log /dev/stdout;`` branch.
_DEV_STDOUT_EMER = re.compile(r'open\(\) "/dev/stdout" failed')
# The watchdog logs this on every non-cooperative nginx exit; absent when the
# fix holds (nginx never exits), repeated on revert (rc=1 crash-loop).
_NGINX_EXITED = re.compile(r"nginx exited \(rc=")


def _systemd_user_available() -> tuple[bool, str]:
    """Whether a real systemd user manager + ``/dev/log`` are usable here.

    Returns ``(ok, reason)``. The suite skips (with ``reason``) on hosts that
    can't exercise the #1727 path: macOS, GitHub Actions runners (no PID-1
    systemd), containers without a user manager, and any host without the
    journald ``/dev/log`` socket the syslog route targets.
    """
    for exe in ("systemd-run", "systemctl", "journalctl"):
        if not shutil.which(exe):
            return False, f"{exe!r} not on PATH"
    if not os.path.exists("/dev/log"):
        return (
            False,
            "/dev/log absent — no systemd-journald dev-log socket, so the "
            "syslog:server=unix:/dev/log route (#1727) cannot be exercised",
        )
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"systemctl --user unreachable: {exc!r}"
    state = r.stdout.strip()
    # 'running' (rc 0) or 'degraded' (rc 1) — both mean the user manager is up
    # and can run transient units. 'offline'/'initializing'/'unknown'/etc. or a
    # connection error means no usable manager (the CI-runner case).
    if state not in ("running", "degraded"):
        return (
            False,
            f"systemd user manager not running (state={state!r}, rc="
            f"{r.returncode}); this needs a real Linux host with a user "
            "systemd manager (e.g. the NixOS dev box), not a CI runner",
        )
    return True, state


def _systemd_setenv_args(env: dict[str, str]) -> list[str]:
    """Build ``--setenv=KEY=VALUE`` argv from an env dict for ``systemd-run``.

    ``systemd-run --user`` does **not** inherit the caller's environment (the
    service gets only the user manager's env plus what ``--setenv`` adds), so
    the hermetic ``clean_env`` that a normal ``Popen`` would get is replayed
    here one var at a time. Two safety filters:

    - **Name**: systemd rejects env-var names outside ``[A-Za-z_][A-Za-z0-9_]*``
      (e.g. bash's exported-function exports ``BASH_FUNC_foo%%``); skip those.
    - **Value**: ``--setenv`` can't safely carry embedded newlines (a multiline
      value would be split / corrupt the unit); skip those too. The config /
      path / port / secret values this suite forwards never contain newlines.
    """
    args: list[str] = []
    for key, val in env.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if "\n" in val or "\r" in val:
            continue
        args.append(f"--setenv={key}={val}")
    return args


def _journal(unit: str) -> str:
    """The full journal for ``unit`` (empty string if journalctl fails)."""
    try:
        r = subprocess.run(
            ["journalctl", "--user-unit", unit, "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_active(unit: str) -> str:
    """``systemctl --user is-active`` result for ``unit`` (``'unknown'` etc.)."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class TestSystemdUserNginx:
    """Run klangkd (nginx engine) under ``systemctl --user`` and assert #1727.

    One transient user service is shared across the assertions (class-scoped
    fixture): it forces ``KLANGKD_PROXY_ENGINE=nginx`` (#1634 made caddy the
    default, which would not exercise this path) and launches
    ``python3 -m klangk.launcher`` with the default ``StandardOutput=journal``
    — i.e. deliberately **not** the ``append:`` workaround from #1546 (the
    whole point of #1727 is that it's no longer needed).
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def service(tmp_path_factory):
        ok, reason = _systemd_user_available()
        if not ok:
            pytest.skip(f"systemd user nginx e2e not runnable here: {reason}")

        tmpdir = tmp_path_factory.mktemp("systemd-nginx")
        data_dir = str(tmpdir / "data")
        state_dir = str(tmpdir / "state")
        os.makedirs(data_dir)
        os.makedirs(state_dir)

        browser_port = str(free_port())
        egress_port = str(free_port())
        # A unique transient unit name per run: xdist-safe, and isolates the
        # journal so assertions read only this run's entries.
        unit = "klangk-1727-" + os.urandom(4).hex()

        # Hermetic env (no ambient KLANGKD_* leak, #1526). The proxy is ENABLED
        # (``_KLANGKD_DISABLE_PROXY=""`` clears the test-suppression default) and
        # the nginx engine is forced explicitly. ``KLANGKD_PORT`` set ⇒
        # full/browser mode, so there's a browser listener to hit /health on.
        env = clean_env(
            KLANGKD_DATA_DIR=data_dir,
            KLANGKD_STATE_DIR=state_dir,
            KLANGKD_LISTEN="127.0.0.1",
            KLANGKD_PORT=browser_port,
            KLANGKD_EGRESS_PORT=egress_port,
            KLANGKD_PROXY_ENGINE="nginx",
            KLANGKD_JWT_SECRET="systemd-nginx-e2e",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="test@example.com",
            KLANGKD_DEFAULT_PASSWORD="testpass",
            KLANGKD_AUTH_MODES="none",
            KLANGKD_TEST_MODE="1",
            KLANGKD_IDLE_TIMEOUT_SECONDS="300",
            KLANGKD_PORT_RANGE_START=str(free_port()),
            _KLANGKD_DISABLE_PROXY="",
            LOGFIRE_TOKEN="",
        )

        cmd = (
            [
                "systemd-run",
                "--user",
                "--unit",
                unit,
                "--service-type=exec",
            ]
            + _systemd_setenv_args(env)
            + ["python3", "-m", "klangk.launcher", "--config=none"]
        )
        # ``systemd-run`` returns once the start job completes (the unit is
        # active or failed). A non-zero rc here means systemd refused to start
        # it (e.g. invalid unit name) — surface that before the readiness poll.
        launch = subprocess.run(cmd, capture_output=True, text=True)
        if launch.returncode != 0:
            pytest.fail(
                f"systemd-run refused to start {unit} (rc="
                f"{launch.returncode}):\n{launch.stderr}"
            )

        url = f"http://127.0.0.1:{browser_port}"
        _start = time.time()
        try:
            deadline = _start + _READINESS_TIMEOUT
            last_err: Exception | None = None
            ready = False
            last_crash_check = 0.0
            while time.time() < deadline:
                # Fail fast if the service died — readiness polling a dead
                # unit for 60s would otherwise mask a startup crash behind a
                # timeout.
                if _is_active(unit) in ("failed", "inactive", "unknown"):
                    raise RuntimeError(
                        f"{unit} did not stay up (is-active="
                        f"{_is_active(unit)!r}):\n{_journal(unit)[-4000:]}"
                    )
                # Fail fast on the #1550 nginx crash-loop too. The service
                # stays 'active' (klangkd is up; only its nginx child dies),
                # so the is-active check above never trips on a reverted
                # #1727 — without this, the reverted case burns the whole
                # readiness timeout before failing. Throttled to once / 3s,
                # after a 2s grace so nginx has had time to spawn + crash.
                now = time.time()
                if now > _start + 2 and now - last_crash_check > 3:
                    last_crash_check = now
                    log = _journal(unit)
                    if _DEV_STDOUT_EMER.search(log):
                        raise RuntimeError(
                            f"{unit}'s nginx is crash-looping on "
                            'open() "/dev/stdout" failed (the #1550 '
                            "regression) — the renderer took the legacy "
                            "/dev/stdout branch under systemd instead of "
                            "the #1727 syslog route:\n"
                            f"{log[-3000:]}"
                        )
                try:
                    if (
                        httpx.get(f"{url}/health", timeout=3).status_code
                        == 200
                    ):
                        ready = True
                        break
                except Exception as exc:  # not serving yet
                    last_err = exc
                time.sleep(0.5)
            if not ready:
                raise RuntimeError(
                    f"{unit} did not serve /health within "
                    f"{_READINESS_TIMEOUT}s (last error: {last_err!r}):\n"
                    f"{_journal(unit)[-4000:]}"
                )

            yield {
                "unit": unit,
                "url": url,
                "browser_port": browser_port,
                "egress_port": egress_port,
            }
        finally:
            # Always stop + reset, even on failure, so no transient unit
            # lingers as 'failed' (and its journal would survive a later run
            # with the same name only if we reused names — we don't, but be
            # tidy). reset-failed is a no-op when the unit isn't failed.
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["systemctl", "--user", "reset-failed", unit],
                capture_output=True,
                timeout=10,
            )

    def test_service_reaches_active(self, service):
        """The service is ``active`` — klangkd is running, not crash-looping."""
        assert _is_active(service["unit"]) == "active"

    def test_proxy_serves_request(self, service):
        """A real request through the browser listener succeeds — nginx is up.

        This is the primary regression guard: if #1727 is reverted, nginx
        crash-loops on ``open() "/dev/stdout"`` and the browser listener never
        serves, so this 200 turns into a connection refused.
        """
        r = httpx.get(f"{service['url']}/health", timeout=10)
        assert r.status_code == 200

    def test_nginx_access_logs_reach_journal(self, service):
        """nginx access-log lines land in the unit's journal over ``/dev/log``.

        The key #1727 assertion: the ``syslog:server=unix:/dev/log`` route
        actually delivers access logs to the journal (not lost). We make a
        fresh request, give journald a beat to flush, then grep the unit's
        journal for the nginx access-log line carrying our request.
        """
        # Make a request whose access-log line we can match uniquely.
        r = httpx.get(f"{service['url']}/health", timeout=10)
        assert r.status_code == 200
        # journald indexing is near-instant but not synchronous; poll briefly.
        deadline = time.time() + 5
        while time.time() < deadline:
            log = _journal(service["unit"])
            if "GET /health HTTP" in log:
                break
            time.sleep(0.25)
        else:
            log = _journal(service["unit"])
        # The access-log line is emitted by the nginx worker (syslog tag
        # 'nginx') and carries the request line — proving the syslog→journal
        # route works end-to-end.
        assert "GET /health HTTP" in log, (
            "no nginx access-log line for GET /health in the unit journal — "
            "the syslog:server=unix:/dev/log route (#1727) is not delivering "
            f"access logs to journald:\n{log[-3000:]}"
        )

    def test_no_dev_stdout_crash_loop_signature(self, service):
        """The journal shows no ``#1550`` crash-loop signature.

        With #1727 in place nginx never fails to ``open()`` its access-log
        destination and never exits, so neither the emerg line nor the
        watchdog's restart log appears. On revert both flood the journal.
        """
        log = _journal(service["unit"])
        emerg = _DEV_STDOUT_EMER.findall(log)
        exited = _NGINX_EXITED.findall(log)
        assert not emerg, (
            "found the #1550 regression signature "
            '(open() "/dev/stdout" failed) in the journal — nginx is '
            "crash-looping because the renderer took the legacy /dev/stdout "
            f"branch under systemd:\n{log[-3000:]}"
        )
        assert not exited, (
            "nginx exited non-cooperatively during the run — the watchdog "
            "logged restart(s), i.e. the #1550 crash-loop is present:\n"
            f"{log[-3000:]}"
        )
