"""Egress filtering against the appliance (#2561).

Two scenarios, both fully black-box:

* **Static deny** — a workspace in ``egress_mode=static`` with an
  allow-list: an off-list DNS lookup is NXDOMAIN'd by the network
  sidecar (no upstream, no consent prompt — deterministic on any
  runner), and the sidecar container is visibly running inside the
  appliance's nested rootless podman.

* **Interactive consent deny** — a workspace in ``egress_mode=
  interactive``: an outbound connection holds at the SYN, the request
  arrives on the ``/ws/consent-decider`` stream (the same protocol the
  shipped consent TUI speaks), a deny verdict resolves it and the
  connection is refused fast (the sidecar's forged RST, #2345).
"""

import json
import uuid

import websockets

from _ws import connect_workspace, exec_command, recv_until


_ALLOW_LIST = ["allowed.local"]


def _make_workspace(api, headers, **fields):
    resp = api.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": f"super-egress-{uuid.uuid4().hex[:8]}",
            "allowed_domains": _ALLOW_LIST,
            **fields,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_static_mode_denies_offlist_dns(appliance, api, auth):
    ws_id = _make_workspace(api, auth["headers"], egress_mode="static")
    conn = await connect_workspace(appliance, auth["token"], ws_id)
    try:
        # Off-list name: the sidecar answers NXDOMAIN itself (static
        # deny) — getent finds no address and exits nonzero (2).
        output, code = await exec_command(
            conn, ["getent", "hosts", f"denied-{uuid.uuid4().hex[:6]}.test"]
        )
        assert code != 0, f"off-list host resolved: {output!r}"
        assert output.strip() == ""
    finally:
        await conn.close()
        api.delete(f"/api/v1/workspaces/{ws_id}", headers=auth["headers"])


async def test_sidecar_runs_inside_appliance(appliance, api, auth):
    """A filtered workspace brings up the network sidecar container (#2301)."""
    ws_id = _make_workspace(api, auth["headers"], egress_mode="static")
    conn = await connect_workspace(appliance, auth["token"], ws_id)
    try:
        result = appliance.exec(
            "podman",
            "ps",
            "--filter",
            "label=klangk.role=network-sidecar",
            "--format",
            "{{.Names}}",
            check=False,
        )
        sidecars = [
            line for line in result.stdout.splitlines() if line.strip()
        ]
        assert sidecars, (
            "no network-sidecar container inside the appliance; podman ps "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    finally:
        await conn.close()
        api.delete(f"/api/v1/workspaces/{ws_id}", headers=auth["headers"])


async def test_interactive_consent_deny(appliance, api, auth):
    """A held request reaches a consent decider; deny refuses fast (#2242).

    The decider here is a raw WS client on ``/ws/consent-decider`` —
    the public protocol the shipped TUI uses (ping / egress_request /
    verdict / egress_resolved), not an in-process app.
    """
    token = auth["token"]
    headers = auth["headers"]
    ws_id = _make_workspace(api, headers, egress_mode="interactive")
    conn = await connect_workspace(appliance, token, ws_id)
    decider = None
    try:
        url = (
            f"{appliance.url.replace('http://', 'ws://')}"
            f"/ws/consent-decider?workspace={ws_id}"
        )
        decider = await websockets.connect(
            url,
            max_size=2**20,
            subprotocols=["bearer", token],
        )

        # Fire the connection inside the workspace; the SYN holds at the
        # sidecar until a verdict. curl retries/timeout bounds the hold.
        # No wrapper shell: exec_exit then carries curl's own exit code.
        # example.com is off the allow-list (allowed.local), so its SYN is
        # held for consent — a real, resolvable host like the backend e2e
        # uses (test_consent_decisions_e2e.py).
        host = "example.com"
        await conn.send(
            json.dumps(
                {
                    "cmd": "exec_start",
                    "command": [
                        "curl",
                        "--max-time",
                        "40",
                        "-sS",
                        f"https://{host}",
                    ],
                }
            )
        )

        # The held request arrives at the decider.
        msgs = await recv_until(
            decider,
            lambda m: (
                m.get("type") == "egress_request"
                and host in (m.get("request", {}).get("dest_host") or "")
            ),
            timeout=60,
        )
        requests = [
            m
            for m in msgs
            if m.get("type") == "egress_request"
            and host in (m.get("request", {}).get("dest_host") or "")
        ]
        assert requests, f"no egress_request for {host}; msgs: {msgs}"
        request_id = requests[0]["request"]["id"]

        # Deny it — the same verdict frame the TUI sends.
        await decider.send(
            json.dumps(
                {
                    "type": "verdict",
                    "request_id": request_id,
                    "decision": "deny",
                    "duration": "5m",
                }
            )
        )
        resolved = await recv_until(
            decider,
            lambda m: (
                m.get("type") == "egress_resolved"
                and m.get("request_id") == request_id
            ),
            timeout=30,
        )
        assert any(
            m.get("type") == "egress_resolved"
            and m.get("request_id") == request_id
            for m in resolved
        ), f"request not resolved; msgs: {resolved}"

        # The denied connection is refused fast (forged RST — exit 7),
        # not a silent allow (exit 0) or a hang (exit 28).
        exits = await recv_until(
            conn,
            lambda m: m.get("type") == "exec_exit",
            timeout=60,
        )
        codes = [m.get("code") for m in exits if m.get("type") == "exec_exit"]
        outputs = [
            m.get("data", "") for m in exits if m.get("type") == "exec_output"
        ]
        assert codes == [7], (
            f"expected curl exit 7 (connection refused), got {codes}; "
            f"output: {outputs}"
        )
    finally:
        if decider is not None:
            await decider.close()
        await conn.close()
        api.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
