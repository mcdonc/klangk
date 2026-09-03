"""E2e: the egress_consent retention/cap pruning machinery (#2303).

Drives the real stack end to end -- klangkd, a workspace container, its
network sidecar -- and verifies the table-bounding machinery on a real
database, not the model unit in isolation:

  1. Real static-denial rows are created by production code: off-list
     ``curl`` inside an interactive-mode workspace with no decider
     connected -> the held SYN relays to klangkd -> the coordinator's
     revert-to-static branch (#2308) -> ``record_static_denial`` rows in
     ``klangk.db``.
  2. A klangkd restart (kill + relaunch on the same data_dir, like a
     crash/power-off) fires the startup sweep immediately.
  3. The sweep deletes exactly what the settings say:
     - retention test: terminal rows backdated past the window go,
       fresh ones stay;
     - row-cap test: the oldest rows are trimmed down to the cap.
  4. The workspace row itself survives (prune never cascades).

The hourly in-process sweep is wall-clock-bounded and unit-tested; the
startup sweep is the timely, restart-driven path this suite exercises.

Run: devenv shell -- test-backend-e2e -k TestConsentPruneE2E
"""

import os
import sqlite3
import subprocess
import time

import pytest

from _e2e_env import close_popen_pipes
from _e2e_server import start_server, stop_server, tracked_mkdtemp

# Common server config: TCP via the proxy (the sidecar's WS back to klangkd
# needs the egress listener), password auth, test mode.
_BASE_ENV = dict(
    KLANGKD_JWT_SECRET="consent-prune-secret",
    KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
    KLANGKD_DEFAULT_USER="prune@example.com",
    KLANGKD_DEFAULT_PASSWORD="prunepass",
    KLANGKD_TEST_MODE="1",
    KLANGKD_IDLE_TIMEOUT_SECONDS="3600",
    LOGFIRE_TOKEN="",
)

_BRINGUP_TIMEOUT = 120  # container create+start+sidecar on a loaded runner
_ROW_TIMEOUT = 90  # off-list curls -> sidecar -> klangkd -> row in the DB
_PRUNE_TIMEOUT = 60  # startup sweep visibility after relaunch


def _login(server: dict) -> dict:
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "prune@example.com", "password": "prunepass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_workspace(server: dict, headers: dict, name: str) -> str:
    # Bring-up calls share _BRINGUP_TIMEOUT, not a fixed per-call budget:
    # under concurrent E2E suites the handler can legitimately spend over
    # 30s in container bring-up before answering, which used to surface as
    # an httpx ReadTimeout instead of a real assertion failure (#3062).
    resp = server["client"].post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": name,
            "allowed_domains": ["allowed.local"],
            # Interactive with no decider ever connected: off-list SYNs reach
            # the coordinator, which records static denial rows (see
            # _trigger). This is the revert-to-static path of #2308.
            "egress_mode": "interactive",
        },
        timeout=_BRINGUP_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["id"]
    resp = server["client"].post(
        f"/api/v1/workspaces/{ws_id}/start",
        headers=headers,
        timeout=_BRINGUP_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    return ws_id


def _container_for_workspace(ws_id: str) -> str | None:
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
    return names[0] if names else None


def _wait_container(ws_id: str) -> str:
    deadline = time.monotonic() + _BRINGUP_TIMEOUT
    while time.monotonic() < deadline:
        name = _container_for_workspace(ws_id)
        if name:
            return name
        time.sleep(2)
    raise RuntimeError(f"workspace container for {ws_id} never appeared")


def _trigger(container: str, host: str) -> None:
    """Detached off-list HTTPS attempt inside the workspace.

    The workspace is ``interactive`` with NO decider connected, so the
    off-list name resolves (interactive mode defers the gate to the SYN,
    #2324), the held SYN reaches klangkd, and the coordinator's not-
    interactive branch records a static denial row + denies at once (#2308
    revert-to-static guarantee) -- the real production path that fills the
    ``egress_consent`` table. (A ``static``-mode workspace would NXDOMAIN
    the name in the sidecar's DNS layer and never produce a row.)
    """
    subprocess.run(
        [
            "podman",
            "exec",
            "-d",
            container,
            "bash",
            "-c",
            f"curl -sS -k --max-time 20 -o /dev/null https://{host} "
            "> /tmp/prune_trig.out 2>&1; true",
        ],
        check=True,
        timeout=15,
    )


def _db(server: dict, *, readonly: bool) -> sqlite3.Connection:
    path = os.path.join(server["data_dir"], "klangk.db")
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _rows(server: dict, ws_id: str) -> list[dict]:
    """The workspace's egress_consent rows, in insertion (rowid) order."""
    con = _db(server, readonly=True)
    try:
        cur = con.execute(
            "SELECT rowid AS rid, id, dest_host, decision, requested_at,"
            " decided_at FROM egress_consent"
            " WHERE workspace_id = ? ORDER BY rowid",
            (ws_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _wait_rows(server: dict, ws_id: str, count: int) -> list[dict]:
    deadline = time.monotonic() + _ROW_TIMEOUT
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = _rows(server, ws_id)
        if len(rows) >= count:
            return rows
        time.sleep(1)
    raise RuntimeError(
        f"only {len(rows)} egress_consent row(s) for {ws_id} "
        f"(wanted {count}) within {_ROW_TIMEOUT}s"
    )


def _wait_row_count(server: dict, ws_id: str, count: int) -> list[dict]:
    """Poll until the row count settles at exactly ``count`` (post-restart
    prune visibility)."""
    deadline = time.monotonic() + _PRUNE_TIMEOUT
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = _rows(server, ws_id)
        if len(rows) == count:
            return rows
        time.sleep(1)
    raise RuntimeError(
        f"egress_consent row count for {ws_id} stayed at {len(rows)} "
        f"(wanted exactly {count}) within {_PRUNE_TIMEOUT}s"
    )


def _set_stamps(server: dict, rows: list[dict], stamps: list[float]) -> None:
    """Write explicit timestamps onto the given rows (server must be down;
    the DB is at rest, so a plain rw connection is safe)."""
    assert len(rows) == len(stamps)
    con = _db(server, readonly=False)
    try:
        for row, stamp in zip(rows, stamps, strict=True):
            con.execute(
                "UPDATE egress_consent SET requested_at = ?,"
                " decided_at = ? WHERE id = ?",
                (stamp, stamp, row["id"]),
            )
        con.commit()
    finally:
        con.close()


def _workspace_exists(server: dict, ws_id: str) -> bool:
    con = _db(server, readonly=True)
    try:
        cur = con.execute("SELECT 1 FROM workspaces WHERE id = ?", (ws_id,))
        return cur.fetchone() is not None
    finally:
        con.close()


def _kill(server: dict) -> None:
    """Kill klangkd like a crash (no graceful drain) and drop its client."""
    proc = server["proc"]
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    close_popen_pipes(proc)
    try:
        server["client"].close()
    except Exception:
        pass
    # A stale UDS socket file must not block the relaunch's bind.
    stale = os.path.join(server["state_dir"], "klangk.sock")
    if os.path.exists(stale):
        os.unlink(stale)


def _relaunch(server: dict, **env: str) -> dict:
    """Relaunch klangkd on the same dirs (fresh ports/client); the startup
    sweep fires immediately on boot.

    ``data_dir``/``state_dir`` must go as named params (the returned
    handle's fields come from them, not from the env overrides), so the
    caller keeps reading the same database.
    """
    return start_server(
        uds=False,
        data_dir=server["data_dir"],
        state_dir=server["state_dir"],
        **env,
    )


def _fresh_dirs() -> tuple[str, str]:
    data_dir = os.path.realpath(tracked_mkdtemp("prune-e2e-data-"))
    state_dir = os.path.realpath(tracked_mkdtemp("prune-e2e-state-"))
    return data_dir, state_dir


class TestConsentPruneE2E:
    @pytest.mark.timeout(600)
    def test_retention_prunes_aged_static_denials(self):
        """Rows older than the retention window are deleted by the startup
        sweep after a restart; fresh rows stay; the workspace row stays."""
        data_dir, state_dir = _fresh_dirs()
        server = start_server(
            uds=False,
            data_dir=data_dir,
            state_dir=state_dir,
            # cap off: only the retention pass runs
            KLANGKD_EGRESS_CONSENT_ROW_CAP="0",
            **_BASE_ENV,
        )
        try:
            headers = _login(server)
            ws_id = _create_workspace(
                server, headers, f"prune-retention-{int(time.time())}"
            )
            container = _wait_container(ws_id)
            # Real, resolvable, off-list hosts: interactive mode defers the
            # gate to the SYN, but the name itself must resolve (a publicly
            # nonexistent name NXDOMAINs at the sidecar's upstream and never
            # produces a SYN -- or a row).
            aged_hosts = ["example.com", "example.net"]
            fresh_hosts = ["example.org", "iana.org"]
            for host in aged_hosts + fresh_hosts:
                _trigger(container, host)
            rows = _wait_rows(server, ws_id, 4)
            assert all(r["decision"] == "denied" for r in rows)

            # rows[] is insertion order (aged hosts fire first). Pick by name
            # when the rows carry hostnames; fall back to oldest-two.
            aged = [r for r in rows if str(r["dest_host"]) in set(aged_hosts)]
            if len(aged) != 2:
                aged = rows[:2]
            fresh = [r for r in rows if r not in aged]

            _kill(server)
            now = time.time()
            _set_stamps(
                server,
                aged,
                [now - 40 * 86400, now - 39 * 86400],  # past 30-day window
            )
            _set_stamps(
                server,
                fresh,
                [now - 100, now - 90],  # well inside the window
            )
            server = _relaunch(server, **_BASE_ENV)

            remaining = _wait_row_count(server, ws_id, 2)
            assert {r["id"] for r in remaining} == {r["id"] for r in fresh}
            assert _workspace_exists(server, ws_id)
        finally:
            stop_server(server)

    @pytest.mark.timeout(600)
    def test_row_cap_trims_oldest_on_restart(self):
        """A workspace over the per-workspace row cap keeps only its newest
        rows after the startup sweep trims the oldest ones."""
        data_dir, state_dir = _fresh_dirs()
        server = start_server(
            uds=False,
            data_dir=data_dir,
            state_dir=state_dir,
            # retention off: only the cap pass runs
            KLANGKD_EGRESS_CONSENT_RETENTION_DAYS="0",
            KLANGKD_EGRESS_CONSENT_ROW_CAP="3",
            **_BASE_ENV,
        )
        try:
            headers = _login(server)
            ws_id = _create_workspace(
                server, headers, f"prune-cap-{int(time.time())}"
            )
            container = _wait_container(ws_id)
            cap_hosts = [
                "example.com",
                "example.net",
                "example.org",
                "iana.org",
                "w3.org",
            ]
            for host in cap_hosts:
                _trigger(container, host)
            rows = _wait_rows(server, ws_id, 5)

            # Pin the trim order deterministically: strictly increasing
            # stamps in insertion order, all inside the (disabled) window.
            _kill(server)
            base = time.time()
            _set_stamps(server, rows, [base - 500 + i * 100 for i in range(5)])
            server = _relaunch(
                server,
                KLANGKD_EGRESS_CONSENT_RETENTION_DAYS="0",
                KLANGKD_EGRESS_CONSENT_ROW_CAP="3",
                **_BASE_ENV,
            )

            remaining = _wait_row_count(server, ws_id, 3)
            assert {r["id"] for r in remaining} == {r["id"] for r in rows[2:]}
            assert _workspace_exists(server, ws_id)
        finally:
            stop_server(server)
