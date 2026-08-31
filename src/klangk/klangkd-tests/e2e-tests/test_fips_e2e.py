"""End-to-end tests for KLANGKD_FIPS_MODE enforcement (#2570, #2591).

Three servers, one per posture:

- **Fail-closed** (FIPS mode ON, *stock* workspace image): every
  workspace start must be refused — 500 from the API, the FIPS reason in
  the server log, no container left behind, and the registry not
  reporting the workspace as running. This is the enforcement teeth:
  a non-FIPS image can never serve under the mode.
- **Passes** (FIPS mode ON, ``klangk-workspace-fips`` image): workspaces
  start, are usable (``klangk exec`` works), and a second start of a
  running workspace is a no-op — the probe is only a gate, not a tax on
  the steady state. Skipped when the FIPS image is not built locally
  (``devenv tasks run klangk:build-fips-image``); CI builds it in the
  e2e-suite step.
- **Control** (FIPS mode OFF, stock image): the probe path is inert —
  the stock image serves as always.

The backend-process audit half is asserted on the fail-closed server's
log: with the mode on, startup must record either a verified FIPS
OpenSSL or the not-enforcing warning (the CI/dev host is non-FIPS, so
the warning is the expected branch there).

Requires: podman, the stock klangk-workspace image (skip otherwise),
and — for the passing class only — klangk-workspace-fips.

Run with: devenv shell -- test-backend-e2e test_fips_e2e.py
"""

import pytest

from _e2e_server import start_server, stop_server

FIPS_IMAGE = "klangk-workspace-fips"

_common = dict(
    KLANGKD_JWT_SECRET="fips-e2e-secret",
    KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
    KLANGKD_DEFAULT_USER="test@example.com",
    KLANGKD_DEFAULT_PASSWORD="testpass",
    KLANGKD_TEST_MODE="1",
    KLANGKD_IDLE_TIMEOUT_SECONDS="300",
    LOGFIRE_TOKEN="",
)


def _image_exists(name: str) -> bool:
    import subprocess

    return (
        subprocess.run(
            ["podman", "image", "exists", name],
            capture_output=True,
        ).returncode
        == 0
    )


def _make_server(**extra):
    return start_server(**_common, **extra)


def _auth(server) -> dict:
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_workspace(server, headers, name: str) -> str:
    resp = server["client"].post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": name},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _log_text(server) -> str:
    """The server's streamed log (file-streamed since #2623)."""
    import os

    log_path = os.path.join(server["data_dir"], "klangkd-test-output.log")
    try:
        with open(log_path) as fh:
            return fh.read()
    except OSError:
        return ""


class TestFipsFailClosed:
    """FIPS mode ON + stock (non-FIPS) image: every start is refused."""

    @pytest.fixture(scope="class")
    @staticmethod
    def server():
        server = _make_server(KLANGKD_FIPS_MODE="1")
        yield server
        stop_server(server)

    def test_start_refused_and_container_reaped(self, server):
        headers = _auth(server)
        ws_id = _create_workspace(server, headers, "fips-fail")

        # The start is refused (PodmanError -> HTTP 500) ...
        resp = server["client"].post(
            f"/api/v1/workspaces/{ws_id}/start", headers=headers, timeout=60
        )
        assert resp.status_code == 500, resp.text

        # ... with the FIPS reason recorded server-side.
        log = _log_text(server)
        assert "failed its FIPS verification" in log, (
            "no FIPS enforcement failure in the klangkd log"
        )
        assert "FIPS provider not enforcing" in log, (
            "the probe verdict (md5 not rejected) should name the cause"
        )
        # And the container was actually reaped — safe_remove swallows
        # podman errors, so a "Failed to reap" warning would mean the
        # container survived the gate (#2626 review).
        assert "Failed to reap non-FIPS workspace container" not in log, (
            "the refused container was not removed"
        )

        # No container left running and the registry does not report it.
        status = server["client"].get(
            f"/api/v1/workspaces/{ws_id}/status",
            headers=headers,
            timeout=30,
        )
        assert status.status_code == 200
        assert status.json()["running"] is False

        # Deterministic, not a fluke: a second start is refused the same
        # way (and does not wedge the workspace).
        resp2 = server["client"].post(
            f"/api/v1/workspaces/{ws_id}/start", headers=headers, timeout=60
        )
        assert resp2.status_code == 500

    def test_backend_audit_line_logged(self, server):
        """The mode's startup audit half: verified-or-warned, never silent."""
        log = _log_text(server)
        assert (
            "FIPS mode enabled" in log
            or "KLANGKD_FIPS_MODE is enabled but" in log
        ), "no FIPS audit line for the klangkd process at startup"


class TestFipsPasses:
    """FIPS mode ON + the FIPS image: workspaces serve normally."""

    @pytest.fixture(scope="class")
    @staticmethod
    def server():
        if not _image_exists(FIPS_IMAGE):
            pytest.skip(
                f"{FIPS_IMAGE} not built — run: "
                "devenv tasks run klangk:build-fips-image"
            )
        server = _make_server(
            KLANGKD_FIPS_MODE="1",
            KLANGKD_IMAGE_NAME=FIPS_IMAGE,
        )
        yield server
        stop_server(server)

    def test_workspace_starts_and_executes(self, server):
        headers = _auth(server)
        ws_id = _create_workspace(server, headers, "fips-pass")

        resp = server["client"].post(
            f"/api/v1/workspaces/{ws_id}/start", headers=headers, timeout=120
        )
        assert resp.status_code == 200, resp.text

        # A 200 start already proves the container is genuinely usable:
        # create_and_start runs several podman execs inside it (sudo
        # config, workspace-token write, the readiness sentinel) plus the
        # FIPS probe itself (python3). A broken image fails one of those.
        status = server["client"].get(
            f"/api/v1/workspaces/{ws_id}/status",
            headers=headers,
            timeout=30,
        )
        assert status.json()["running"] is True

        # Steady state is not taxed: starting the running workspace is a
        # no-op (the probe gates only the fresh-create path).
        resp2 = server["client"].post(
            f"/api/v1/workspaces/{ws_id}/start", headers=headers, timeout=60
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "already_running"

    def test_healthy_audit_line_logged(self, server):
        log = _log_text(server)
        assert (
            "FIPS mode enabled" in log
            or "KLANGKD_FIPS_MODE is enabled but" in log
        )


class TestFipsOffControl:
    """FIPS mode OFF: the probe path is inert for the stock image."""

    @pytest.fixture(scope="class")
    @staticmethod
    def server():
        server = _make_server()
        yield server
        stop_server(server)

    def test_stock_image_serves_normally(self, server):
        headers = _auth(server)
        ws_id = _create_workspace(server, headers, "fips-off")

        resp = server["client"].post(
            f"/api/v1/workspaces/{ws_id}/start", headers=headers, timeout=120
        )
        assert resp.status_code == 200, resp.text

        log = _log_text(server)
        assert "failed its FIPS verification" not in log
        # No audit line when the mode is off.
        assert "FIPS mode enabled" not in log
        assert "KLANGKD_FIPS_MODE is enabled but" not in log
