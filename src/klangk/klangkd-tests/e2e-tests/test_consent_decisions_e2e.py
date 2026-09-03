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

from _e2e_env import ci_budget
from _e2e_server import start_server, stop_server
from test_agent_home_e2e import ws_connect

from klangk.cli.tui.consent import (
    ConsentDeciderApp,
    DECISION_ALLOWED,
    DECISION_DENIED,
    DURATION_5M,
    DURATION_FOREVER,
    DURATION_ONCE,
    DURATION_TILRESTART,
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
        # #3064: full bring-up under four-suite CI contention can outrun
        # a 60s local-dev budget (same family as #2745's doubling).
        timeout=ci_budget(60, 240),
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
    app: ConsentDeciderApp, rid: str, timeout: float = 15, settle: float = 2.5
) -> None:
    """Wait until ``rid`` is gone from pending AND stays gone.

    A single absent poll passes falsely on a decider reconnect: ``reset()``
    clears pending, then the server's snapshot re-adds still-held rows. We
    require CONTINUOUS absence for ``settle`` seconds (past a reconnect-
    snapshot cycle), so only a genuine resolve satisfies this -- once the
    row leaves the coordinator's ``_holds`` the snapshot can't replay it, so
    its absence is permanent; a reset-clear's absence is transient (the
    snapshot re-adds it, resetting the timer).
    """
    deadline = time.time() + timeout
    absent_since: float | None = None
    while time.time() < deadline:
        if rid not in app.controller.pending:
            if absent_since is None:
                absent_since = time.time()
            if time.time() - absent_since >= settle:
                return
        else:
            absent_since = None  # reappeared (reconnect snapshot re-added it)
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"request {rid} not resolved within {timeout}s "
        f"(pending={list(app.controller.pending)})"
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
                app._decide_id(rid, DECISION_ALLOWED, DURATION_TILRESTART)
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
                app._decide_id(rid, DECISION_DENIED, DURATION_TILRESTART)
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
                app._decide_id(rid1, DECISION_ALLOWED, DURATION_TILRESTART)
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
                await pilot.pause()

                result = await _wait_result(container, "/tmp/r_timeout.out")

            # HYPOTHESIZED END STATE: with no human decision, the server
            # auto-denies (fail-close) at the timeout -> the original request
            # is gone from the decider AND the connection did NOT succeed (no
            # silent allow). Timeout surfaces as refuse or connect-timeout
            # depending on the retransmit race; either way exit != 0.
            #
            # We assert on the ORIGINAL request's resolution (_wait_resolved
            # above), not the whole pending list: the forged RST (#2345) makes
            # curl fail-fast on each resolved IP, so a multi-IP host (example.com
            # is a CDN) cascades -- IP1 denied -> curl tries IP2 -> a SECOND
            # held request -- each timing out on its own. Those cascade rows
            # are expected per-IP behavior, not a leak of the original.
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
                app._decide_id(
                    rid_allow, DECISION_ALLOWED, DURATION_TILRESTART
                )
                app._decide_id(rid_deny, DECISION_DENIED, DURATION_TILRESTART)
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

    @pytest.mark.asyncio
    async def test_once_reprompts_each_subsequent_connection(
        self, server, auth, workspace
    ):
        # #2361: an allow with duration=once must re-prompt EVERY subsequent
        # connection to the same host. The sidecar's verdict cache is keyed on
        # the connection (source port), so a new connection (new source port)
        # is a cache miss and re-prompts -- a prior allow-once never carries
        # over to a later connection.
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="once-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                seen_rids = []
                for i in range(3):
                    outfile = f"/tmp/r_once_{i}.out"
                    _trigger(container, "example.com", outfile)
                    rid = await _wait_for_request(app, "example.com")
                    # Each new connection produces a DISTINCT prompt -- a
                    # prior allow-once must not be reused for a later curl.
                    assert rid not in seen_rids, (
                        f"iteration {i} reused prior request {rid}; "
                        "once must re-prompt each new connection"
                    )
                    seen_rids.append(rid)
                    app._decide_id(rid, DECISION_ALLOWED, DURATION_ONCE)
                    await _wait_resolved(app, rid)
                    await pilot.pause()
                results = [
                    await _wait_result(container, f"/tmp/r_once_{i}.out")
                    for i in range(3)
                ]
            # Every allow-once connection was released and succeeded.
            assert all("EXIT:0" in r for r in results), (
                f"each allow-once connection should succeed; got: {results!r}"
            )
        finally:
            await ws_conn.close()

    @pytest.mark.asyncio
    async def test_forever_deny_persists_without_reprompting(
        self, server, auth, workspace
    ):
        # Inverse of test_once_reprompts_each_subsequent_connection: a deny
        # with duration=forever PERSISTS across connections. A later curl to
        # the same host is refused fast (ECONNREFUSED, exit 7) WITHOUT a new
        # consent prompt -- the sidecar's long-lived REJECT rule (top of
        # OUTPUT, ahead of NFQUEUE) catches the new SYN. This pins that
        # connection-keying the verdict cache (#2361) did not break
        # duration-bounded persistence: "forever" still holds across
        # connections via the rule, even though the per-connection cache no
        # longer reuses the verdict for a new source port.
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="forever-deny-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                # 1st connection: held, then denied forever.
                _trigger(container, "example.com", "/tmp/r_forever_0.out")
                rid = await _wait_for_request(app, "example.com")
                app._decide_id(rid, DECISION_DENIED, DURATION_FOREVER)
                await _wait_resolved(app, rid)
                await pilot.pause()
                # Wait for the 1st curl to fail fast (ECONNREFUSED via the forged
                # RST -- confirms the sidecar processed the verdict) and let the
                # forever-REJECT rule settle into OUTPUT before the 2nd curl: the
                # rule installs in a worker thread that lags the decider's
                # egress_resolved broadcast, so an instant 2nd SYN would race it.
                res0 = await _wait_result(container, "/tmp/r_forever_0.out")
                assert "EXIT:7" in res0, (
                    f"the denied connection should be refused fast, got: {res0!r}"
                )
                await asyncio.sleep(2)
                # 2nd connection: the forever-deny REJECT rule must refuse it
                # fast with NO new prompt. Watch the decider's in-memory
                # pending set for a short window (no blocking subprocess in the
                # loop, so the decider WS stays alive); a prompt here is the
                # regression. The new SYN never reaches NFQUEUE (the REJECT
                # rule, top of OUTPUT, RSTs it first), so no prompt arrives.
                _trigger(container, "example.com", "/tmp/r_forever_1.out")
                prompted = False
                deadline = time.time() + 8
                while time.time() < deadline:
                    if any(
                        "example.com" in (r.dest_host or "")
                        for r in app.controller.pending.values()
                    ):
                        prompted = True
                        break
                    await asyncio.sleep(0.3)
                res1 = await _wait_result(container, "/tmp/r_forever_1.out")
            assert not prompted, (
                "forever-deny must not re-prompt a later connection "
                "(persistence is via the REJECT rule, not the verdict cache)"
            )
            assert "EXIT:7" in res1, (
                f"the later connection should be refused fast (ECONNREFUSED) "
                f"by the forever-deny REJECT rule, got: {res1!r}"
            )
        finally:
            await ws_conn.close()

    @pytest.mark.asyncio
    async def test_timed_allow_persists_without_reprompting(
        self, server, auth, workspace
    ):
        # #2399 Finding 1: a TIMED allow (5m/1h/5s) must cover a later
        # connection to the same host WITHOUT re-prompting -- the same way a
        # timed deny does. Like `forever`, a timed allow now host-scopes via
        # the in-session _SESSION_HOST_ALLOWS gate in _cb + the DNS path
        # (#2434); a new connection is ALSO covered by the learned iptables
        # ACCEPT rule (top of OUTPUT, ahead of NFQUEUE) installed by
        # allow(dst, None, ttl). This test pins the no-re-prompt behavior.
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="timed-allow-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                # 1st connection: held, then allowed for 5m (timed). A raw IP
                # (1.1.1.1) is used so DNS/CDN rotation can't change the dst
                # between connections -- a re-prompt here is unambiguously the
                # learned ACCEPT rule failing to cover the 2nd SYN (#2399).
                _trigger(container, "1.1.1.1", "/tmp/r_tallow_0.out")
                rid = await _wait_for_request(app, "1.1.1.1")
                app._decide_id(rid, DECISION_ALLOWED, DURATION_5M)
                await _wait_resolved(app, rid)
                await pilot.pause()
                # Wait for the 1st curl to succeed (confirms the verdict was
                # applied) and let the learned ACCEPT rule settle into OUTPUT
                # before the 2nd curl: the rule installs in a worker thread
                # that lags the decider's egress_resolved broadcast, so an
                # instant 2nd SYN would race it.
                res0 = await _wait_result(container, "/tmp/r_tallow_0.out")
                assert "EXIT:0" in res0, (
                    f"the allowed connection should succeed, got: {res0!r}"
                )
                await asyncio.sleep(2)
                # 2nd connection: the timed-allow ACCEPT rule must pass it
                # with NO new prompt. Watch the decider's in-memory pending
                # set for a short window; a prompt here is the regression
                # (#2399: the new SYN reached NFQUEUE because the learned
                # ACCEPT did not cover it).
                _trigger(container, "1.1.1.1", "/tmp/r_tallow_1.out")
                prompted = False
                deadline = time.time() + 8
                while time.time() < deadline:
                    if any(
                        "1.1.1.1" in (r.dest_host or "")
                        for r in app.controller.pending.values()
                    ):
                        prompted = True
                        break
                    await asyncio.sleep(0.3)
                res1 = await _wait_result(container, "/tmp/r_tallow_1.out")
            assert not prompted, (
                "timed-allow must not re-prompt a later connection "
                "(persistence is via the learned ACCEPT rule, #2399)"
            )
            assert "EXIT:0" in res1, (
                f"the later connection should succeed under the timed-allow "
                f"ACCEPT rule, got: {res1!r}"
            )
        finally:
            await ws_conn.close()

    @pytest.mark.asyncio
    async def test_restart_deny_persists_then_clears_on_container_restart(
        self, server, auth, workspace
    ):
        # A deny with duration=restart is bounded to the workspace container's
        # lifetime: it persists while the container runs (a later curl is
        # auto-rejected, no re-prompt) but does NOT survive a container
        # restart -- the sidecar's in-memory REJECT rule dies with the old
        # container and klangkd reaps the row (clear_restart_duration, #2346).
        # After a restart, a curl re-prompts.
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="restart-deny-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                # 1st connection: held, then denied until restart.
                _trigger(container, "example.com", "/tmp/r_restart_0.out")
                rid = await _wait_for_request(app, "example.com")
                app._decide_id(rid, DECISION_DENIED, DURATION_TILRESTART)
                await _wait_resolved(app, rid)
                await pilot.pause()
                res0 = await _wait_result(container, "/tmp/r_restart_0.out")
                assert "EXIT:7" in res0, (
                    f"the denied connection should be refused fast, "
                    f"got: {res0!r}"
                )
                await asyncio.sleep(2)
                # 2nd connection: the restart-deny REJECT rule persists for
                # the container's lifetime -> refused fast, NO new prompt.
                _trigger(container, "example.com", "/tmp/r_restart_1.out")
                prompted = False
                deadline = time.time() + 8
                while time.time() < deadline:
                    if any(
                        "example.com" in (r.dest_host or "")
                        for r in app.controller.pending.values()
                    ):
                        prompted = True
                        break
                    await asyncio.sleep(0.3)
                res1 = await _wait_result(container, "/tmp/r_restart_1.out")
                assert not prompted, (
                    "restart-deny must not re-prompt while the container runs"
                )
                assert "EXIT:7" in res1, (
                    f"2nd connection should be refused by the persisted "
                    f"REJECT rule, got: {res1!r}"
                )
                # Restart the workspace container. stop_and_remove tears down
                # the sidecar (its in-memory REJECT rule dies); start_container
                # starts a fresh sidecar + reaps restart-duration rows (#2346).
                # Run the blocking POST in a thread so the decider's WS loop
                # stays alive (a ~30s blocking call would trip its keepalive).
                restart = await asyncio.to_thread(
                    server["client"].post,
                    f"/api/v1/workspaces/{ws_id}/restart",
                    headers=auth["headers"],
                    timeout=ci_budget(120, 240),
                )
                assert restart.status_code == 200, restart.text
                # The pre-restart workspace WS is stale; wait on a fresh one
                # for the new container + sidecar to come back.
                await ws_conn.close()
                ws_conn = await ws_connect(server, auth, ws_id)
                await asyncio.sleep(
                    2
                )  # let the fresh sidecar's dns-proxy settle
                container = _container_for_workspace(ws_id)
                # 3rd connection: the restart-deny did NOT survive the restart
                # -> a fresh egress request is held for a decision.
                _trigger(container, "example.com", "/tmp/r_restart_2.out")
                rid2 = await _wait_for_request(app, "example.com")
                assert rid2 != rid, (
                    "a connection after restart must produce a fresh prompt, "
                    "not reuse the reaped request"
                )
                # Clean up: deny so the held connection doesn't outlive the test.
                app._decide_id(rid2, DECISION_DENIED, DURATION_TILRESTART)
                await _wait_resolved(app, rid2)
        finally:
            await ws_conn.close()

    @pytest.mark.asyncio
    async def test_forever_allow_persists_across_container_restart(
        self, server, auth, workspace
    ):
        # #2364: an allow with duration=forever persists across a workspace
        # container restart. Unlike `restart` (bounded to the container's
        # lifetime -- its in-memory rule dies and the row is reaped on restart,
        # see test_restart_deny_persists_then_clears_on_container_restart), a
        # `forever` allow mutates the workspace's allowed_domains (#2368),
        # which the fresh sidecar re-reads on start. So a post-restart curl to
        # the same host is allowed WITHOUT a new consent prompt.
        ws_id = workspace
        ws_conn = await ws_connect(server, auth, ws_id)
        try:
            container = _container_for_workspace(ws_id)
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="forever-allow-test",
                hold_timeout=_CONSENT_TIMEOUT,
            )
            async with app.run_test() as pilot:
                await _wait_connected(app)
                # 1st connection: held, then allowed forever. The deciding
                # connection succeeds (in-memory ACCEPT); #2368 also appends
                # example.com:443 to the workspace's allowed_domains.
                _trigger(container, "example.com", "/tmp/r_fa_0.out")
                rid = await _wait_for_request(app, "example.com")
                app._decide_id(rid, DECISION_ALLOWED, DURATION_FOREVER)
                await _wait_resolved(app, rid)
                await pilot.pause()
                res0 = await _wait_result(container, "/tmp/r_fa_0.out")
                assert "EXIT:0" in res0, (
                    f"the allowed connection should succeed, got: {res0!r}"
                )
                # 2nd connection (pre-restart): a `forever` allow approves the
                # whole domain, so a later curl -- even one that resolves to a
                # CDN-rotated IP -- must NOT re-prompt (#2372: the sidecar
                # allow-lists the host in-session via _SESSION_HOST_ALLOWS, so the DNS
                # path learns every resolved IP and allows it without NFQUEUE).
                await asyncio.sleep(2)
                _trigger(container, "example.com", "/tmp/r_fa_1.out")
                prompted = False
                deadline = time.time() + 8
                while time.time() < deadline:
                    if any(
                        "example.com" in (r.dest_host or "")
                        for r in app.controller.pending.values()
                    ):
                        prompted = True
                        break
                    await asyncio.sleep(0.3)
                res1 = await _wait_result(container, "/tmp/r_fa_1.out")
                assert not prompted, (
                    "a forever-allow must not re-prompt a later connection to "
                    "the same domain (a rotated IP included) -- #2372"
                )
                assert "EXIT:0" in res1, (
                    f"2nd connection should succeed without a re-prompt, "
                    f"got: {res1!r}"
                )
                # Restart the workspace container. The sidecar dies (its
                # in-memory learned ACCEPT dies with it) and a fresh sidecar
                # starts, which re-reads allowed_domains -- now containing
                # example.com:443 (#2368). Run the blocking POST in a thread
                # so the decider's WS loop stays alive.
                restart = await asyncio.to_thread(
                    server["client"].post,
                    f"/api/v1/workspaces/{ws_id}/restart",
                    headers=auth["headers"],
                    timeout=ci_budget(120, 240),
                )
                assert restart.status_code == 200, restart.text
                await ws_conn.close()
                ws_conn = await ws_connect(server, auth, ws_id)
                await asyncio.sleep(
                    2
                )  # let the fresh sidecar's dns-proxy settle
                container = _container_for_workspace(ws_id)
                # 3rd connection (post-restart): the forever-allow SURVIVED the
                # restart via allowed_domains -> allowed WITHOUT a new prompt.
                # The fresh sidecar allow-lists example.com by NAME (whatever
                # IP DNS returns), so this is robust to CDN rotation. Pre-#2368
                # this re-prompted: the learned ACCEPT died and nothing
                # re-applied it.
                _trigger(container, "example.com", "/tmp/r_fa_2.out")
                prompted2 = False
                deadline = time.time() + 8
                while time.time() < deadline:
                    if any(
                        "example.com" in (r.dest_host or "")
                        for r in app.controller.pending.values()
                    ):
                        prompted2 = True
                        break
                    await asyncio.sleep(0.3)
                res2 = await _wait_result(container, "/tmp/r_fa_2.out")
                assert not prompted2, (
                    "forever-allow must persist across a container restart "
                    "(via allowed_domains, #2368) -- a post-restart curl must "
                    "not re-prompt"
                )
                assert "EXIT:0" in res2, (
                    f"the post-restart connection should succeed without a "
                    f"prompt (allowed_domains survived), got: {res2!r}"
                )
        finally:
            await ws_conn.close()
