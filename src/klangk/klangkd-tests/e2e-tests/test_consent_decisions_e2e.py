"""E2e: end states after realistic consent-decider interactions (#2327).

Four scenarios driven through the real stack (klangkd + a workspace container
+ its network sidecar), with the real ConsentDeciderApp piloted in-process.
Each triggers a real outbound connection from inside the workspace, observes
the held request arrive in the decider, performs a decision a user would
make, and asserts the resulting end state.

  1. allow              -> the connection succeeds (curl exit 0), request resolves
  2. deny               -> connection refused fast (curl exit 7, ECONNREFUSED
                         via the forged RST #2345), request resolves
  3. allow one host     -> a *different* host is still independently held
                          (per-host isolation; decisions aren't global)
  4. no decision        -> the consent timeout auto-denies; request auto-removes
                           and the connection does NOT succeed (no silent allow)
  5. two concurrent     -> both held at once; allow one + deny the other -> each
                          gets its own verdict's outcome (no cross-talk)

Tests 1-2 state the hypothesized end state first, then drive to it.
Tests 3-5 drive the interactions first, then assert the hypothesized end state.

Run: devenv shell -- test-backend-e2e -k TestConsentDecisionEndStates
"""

import asyncio
import subprocess
import time

import pytest

from _e2e_server import start_server, stop_server
from test_agent_home_e2e import ws_connect

from klangk.cli.tui.consent import (
    ConsentDeciderApp,
    DECISION_ALLOWED,
    DECISION_DENIED,
    DURATION_RESTART,
)

# Short consent timeout so test 4 (no-decision -> timeout) doesn't keep the
# suite waiting. Tests 1-3 decide within a few seconds, well under this.
_CONSENT_TIMEOUT = 12


@pytest.fixture(scope="module")
def server():
    server = start_server(
        uds=False,
        KLANGKD_JWT_SECRET="consent-dec-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_ALLOW_AUTOSTART="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_EGRESS_CONSENT_TIMEOUT=str(_CONSENT_TIMEOUT),
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


@pytest.fixture
def workspace(server, auth):
    resp = server["client"].post(
        "/api/v1/workspaces",
        headers=auth["headers"],
        json={
            "name": f"consent-dec-{int(time.time() * 1000) % 100000}",
            "allowed_domains": ["allowed.local"],
            "egress_mode": "interactive",
            "auto_start": True,
        },
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["id"]
    yield ws_id
    try:
        server["client"].delete(
            f"/api/v1/workspaces/{ws_id}",
            headers=auth["headers"],
            timeout=30,
        )
    except Exception:
        pass


# --- helpers ---------------------------------------------------------------


def _container_for_workspace(ws_id: str) -> str:
    r = subprocess.run(
        [
            "podman",
            "ps",
            "--filter",
            f"label=klangk.workspace={ws_id}",
            "--filter",
            "label=klangk.role=workspace",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    names = [n for n in r.stdout.splitlines() if n.strip()]
    assert names, f"no workspace container found for {ws_id}"
    return names[0]


def _trigger(container: str, host: str, outfile: str) -> None:
    """Fire a detached HTTPS connection inside the workspace; it blocks on the
    held SYN until a verdict, then writes curl's full output + exit code to
    ``outfile``. No ``-s``/``-o /dev/null`` so curl's error text (connection
    refused, timeout) is captured for the assertions."""
    subprocess.run(
        [
            "podman",
            "exec",
            "-d",
            container,
            "bash",
            "-c",
            f"curl --max-time 25 https://{host} > {outfile} 2>&1; "
            f'echo "EXIT:$?" >> {outfile}',
        ],
        check=True,
        timeout=15,
    )


def _result(container: str, outfile: str) -> str:
    r = subprocess.run(
        ["podman", "exec", container, "cat", outfile],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return r.stdout


async def _wait_result(
    container: str, outfile: str, timeout: float = 30
) -> str:
    """Poll the result file until curl has exited (its ``EXIT:$?`` line is
    written), then return the full output. The progress meter is flushed to
    the file *before* curl exits, so waiting for content alone races -- a
    denied connection's fast ECONNREFUSED (#2345) made curl write the meter
    before the test read it. Require the ``EXIT:`` marker so the exit code is
    present. Outlasts curl's 25s max-time."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = _result(container, outfile)
        if "EXIT:" in last:
            return last
        await asyncio.sleep(0.5)
    return last


async def _wait_connected(app: ConsentDeciderApp, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app._connected:
            # The WS is up, but give klangkd a beat to run the decider
            # registration (handle_consent_decider) before we trigger a
            # connection -- otherwise the workspace may still be in static
            # mode and the SYN NXDOMAINs instead of being held for consent.
            await asyncio.sleep(1.5)
            return
        await asyncio.sleep(0.1)
    raise AssertionError("consent-decide TUI did not connect")


async def _wait_for_request(
    app: ConsentDeciderApp, host_substr: str, timeout: float = 30
) -> str:
    """Wait until a pending request whose dest_host contains ``host_substr``
    arrives; return its request id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for req in app.controller.pending.values():
            if host_substr in (req.dest_host or ""):
                return req.id
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"no consent request for '{host_substr}' within {timeout}s "
        f"(pending={list(app.controller.pending)})"
    )


async def _wait_resolved(
    app: ConsentDeciderApp, rid: str, timeout: float = 15
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rid not in app.controller.pending:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"request {rid} not resolved within {timeout}s "
        f"(pending={list(app.controller.pending)})"
    )


async def _wait_pending_empty(
    app: ConsentDeciderApp, timeout: float = 10
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not app.controller.pending:
            return
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"pending not empty within {timeout}s ({dict(app.controller.pending)})"
    )


# --- tests -----------------------------------------------------------------


class TestConsentDecisionEndStates:
    # ====================================================================
    # Test 1 — HYPOTHESIZE END STATE FIRST, then drive to it.
    # ====================================================================
    @pytest.mark.asyncio
    async def test_allow_succeeds_and_resolves(self, server, auth, workspace):
        # HYPOTHESIS: when the user allows a held request, the sidecar releases
        # the SYN, the connection completes, and the request is cleared from
        # the decider. End state: the request is gone from pending AND the
        # connection got a real HTTP response (not refused).
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="allow-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                _trigger(container, "example.com", "/tmp/r_allow.out")

                rid = await _wait_for_request(app, "example.com")
                # The user allows (default duration).
                app._decide_id(rid, DECISION_ALLOWED, DURATION_RESTART)
                await _wait_resolved(app, rid)
                await pilot.pause()

                # Give the released connection time to complete the fetch.
                await asyncio.sleep(5)
                result = _result(container, "/tmp/r_allow.out")

            # END STATE: connection succeeded (got an HTTP status) + resolved.
            assert "EXIT:0" in result, (
                f"expected curl exit 0 (connection succeeded after allow), "
                f"got: {result!r}"
            )
        finally:
            await ws_conn.close()

    # ====================================================================
    # Test 2 — HYPOTHESIZE END STATE FIRST, then drive to it.
    # ====================================================================
    @pytest.mark.asyncio
    async def test_deny_resolves_and_blocks(self, server, auth, workspace):
        # HYPOTHESIS: when the user denies a held request, the connection is
        # refused fast (the sidecar forges a RST directly #2345, so connect()
        # gets ECONNREFUSED at once rather than the ~127s tcp_syn_retries)
        # and the request is cleared from the decider. End state: the request
        # is gone from pending AND curl exited 7 (Connection refused) -- not
        # exit 0 (success) and not exit 28 (connect timeout).
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="deny-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                _trigger(container, "example.com", "/tmp/r_deny.out")

                rid = await _wait_for_request(app, "example.com")
                # The user denies.
                app._decide_id(rid, DECISION_DENIED, DURATION_RESTART)
                await _wait_resolved(app, rid)
                await pilot.pause()

                result = await _wait_result(container, "/tmp/r_deny.out")

            # END STATE: request resolved + the connection was refused fast
            # (ECONNREFUSED, exit 7) -- the forged RST (#2345), not a timeout.
            assert "EXIT:7" in result, (
                f"a denied connection should be refused fast (exit 7), "
                f"got: {result!r}"
            )
        finally:
            await ws_conn.close()

    # ====================================================================
    # Test 3 — CODE FIRST (drive the interactions), then assert the
    # hypothesized end state.
    # ====================================================================
    @pytest.mark.asyncio
    async def test_allow_one_host_does_not_open_another(
        self, server, auth, workspace
    ):
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="isolation-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)

                # Allow a connection to example.com.
                _trigger(container, "example.com", "/tmp/iso1.out")
                rid1 = await _wait_for_request(app, "example.com")
                app._decide_id(rid1, DECISION_ALLOWED, DURATION_RESTART)
                await _wait_resolved(app, rid1)
                await pilot.pause()

                # Now connect to a DIFFERENT, non-allowed host.
                _trigger(container, "cloudflare.com", "/tmp/iso2.out")

                # HYPOTHESIZED END STATE: allowing example.com did NOT open
                # cloudflare.com -- the distinct host is independently held
                # for consent (decisions are per-host, not global). Using two
                # different hosts avoids the verdict-cache / CDN-rotation
                # confounds of re-connecting the same host.
                rid2 = await _wait_for_request(app, "cloudflare.com")
                assert rid2 != rid1
        finally:
            await ws_conn.close()

    # ====================================================================
    # Test 4 — CODE FIRST (drive the interactions), then assert the
    # hypothesized end state.
    # ====================================================================
    @pytest.mark.asyncio
    async def test_no_decision_times_out_and_blocks(
        self, server, auth, workspace
    ):
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="timeout-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)

                # A connection is held; the user does NOTHING.
                _trigger(container, "example.com", "/tmp/r_timeout.out")
                rid = await _wait_for_request(app, "example.com")

                # Wait past the consent timeout for the server to auto-deny.
                await _wait_resolved(app, rid, timeout=_CONSENT_TIMEOUT + 10)
                await _wait_pending_empty(app, timeout=5)
                await pilot.pause()

                result = await _wait_result(container, "/tmp/r_timeout.out")

            # HYPOTHESIZED END STATE: with no human decision, the server
            # auto-denies (fail-close) at the timeout -> the request is gone
            # from the decider AND the connection did NOT succeed (no silent
            # allow). Timeout surfaces as refuse or connect-timeout depending
            # on the retransmit race; either way exit != 0.
            assert not app.controller.pending, (
                "request should have auto-expired out of the decider"
            )
            assert "EXIT:0" not in result, (
                f"a timed-out connection should not succeed, got: {result!r}"
            )
        finally:
            await ws_conn.close()

    # ====================================================================
    # Test 5 — CODE FIRST (drive the interactions), then assert the
    # hypothesized end state. Two requests in flight simultaneously.
    # ====================================================================
    @pytest.mark.asyncio
    async def test_concurrent_allow_one_deny_other(
        self, server, auth, workspace
    ):
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="concurrent-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)

                # Fire TWO connections concurrently; both are held at once.
                _trigger(container, "example.com", "/tmp/conc_allow.out")
                _trigger(container, "cloudflare.com", "/tmp/conc_deny.out")
                rid_allow = await _wait_for_request(app, "example.com")
                rid_deny = await _wait_for_request(app, "cloudflare.com")
                assert rid_allow != rid_deny, (
                    "the two hosts must produce distinct request ids"
                )
                # Both are still pending -- genuinely in flight simultaneously.
                assert rid_allow in app.controller.pending
                assert rid_deny in app.controller.pending

                # The user allows one and denies the OTHER while both are held.
                app._decide_id(rid_allow, DECISION_ALLOWED, DURATION_RESTART)
                app._decide_id(rid_deny, DECISION_DENIED, DURATION_RESTART)
                await _wait_resolved(app, rid_allow)
                await _wait_resolved(app, rid_deny)
                await pilot.pause()

                res_allow = await _wait_result(
                    container, "/tmp/conc_allow.out"
                )
                res_deny = await _wait_result(container, "/tmp/conc_deny.out")

            # HYPOTHESIZED END STATE: each connection got its OWN verdict's
            # outcome -- the allowed host succeeded, the denied host did not.
            # This confirms verdicts route to the correct request under
            # concurrency (allowing one does not allow the other; denying one
            # does not deny the other).
            assert "EXIT:0" in res_allow, (
                f"the allowed host (example.com) should succeed, got: "
                f"{res_allow!r}"
            )
            assert "EXIT:0" not in res_deny, (
                f"the denied host (cloudflare.com) should not succeed, got: "
                f"{res_deny!r}"
            )
        finally:
            await ws_conn.close()
