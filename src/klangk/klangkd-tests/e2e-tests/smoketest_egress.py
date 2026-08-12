#!/usr/bin/env python3
"""Interactive-egress fuzz smoketest (#2392).

Standalone, human-run, not in CI (no ``test_`` prefix). Invoke under devenv:

    devenv --quiet shell -- \\
        python src/klangk/klangkd-tests/e2e-tests/smoketest_egress.py [--count N]

Brings up a real klangkd, creates a workspace with an allow-list + interactive
egress, attaches the real ConsentDeciderApp decider, then for N fuzzed
destinations: ``podman exec`` a curl, wait for the consent request if one is
expected, respond with a fuzzed verdict (allow/deny/timeout x duration), and
record BOTH the sidecar enforcement outcome and the curl result. It models
verdict carryover across iterations, prints a realtime expected-vs-actual line,
and stops on the first hard mismatch (``--continue`` to defer to a summary).
Genuinely under-specified cases (raw IPs, edge-case host formatting) are
reported as findings (``⚠``), not halts.

A mismatch = a contradiction of a deterministic invariant: an uncovered
destination must produce a request; allow/allow-list/active-allow must release
(not refuse); deny/active-deny must refuse (exit 7); no-response must not
succeed (exit != 0); a covered destination must produce no request.

See https://github.com/mcdonc/klangk/issues/2392.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field

# Import sibling e2e helpers regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import httpx  # noqa: E402

from _e2e_server import start_server, stop_server, ws_connect  # noqa: E402

from klangk.cli.tui.consent import (  # noqa: E402
    ConsentDeciderApp,
    DECISION_ALLOWED,
    DECISION_DENIED,
    DURATION_5M,
    DURATION_5S,
    DURATION_FOREVER,
    DURATION_1H,
    DURATION_ONCE,
    DURATION_TILRESTART,
    make_revoke,
    make_verdict,
)

# -- status vocabulary -------------------------------------------------------
PASS = "PASS"
MISMATCH = "MISMATCH"  # hard contradiction of a deterministic invariant
FINDING = "FINDING"  # unexpected but in an exploratory/under-specified area

EXIT_REFUSED = 7  # CURLE_COULDNT_CONNECT -- the forged RST (#2345)

EXPECT_RELEASED = "released"  # allow / allow-list / active-allow -> exit 0
EXPECT_REFUSED = "refused"  # deny / active-deny -> exit 7
EXPECT_NOT0 = "not0"  # no-response (timeout) -> exit != 0

# -- fuzz corpus -------------------------------------------------------------
# Real, resolvable, 443-open+TLS destinations: an allowed conn reaches exit 0
# deterministically and a denied one reaches exit 7 (forged RST). example.com is
# seeded onto the allow-list; the rest are off-list. Raw IPs are exploratory.
_ALLOW_LIST = ["example.com"]
# A workspace-level deny-list baked into the main workspace (#2367). A rejected
# host is pre-emptively denied at the sidecar -- no consent request is ever
# surfaced, even in interactive mode. kernel.org is fresh (not in the fuzz pool
# or any other phase), so baking it in can't perturb the rest of the run.
_REJECTED_LIST = ["kernel.org"]
_POOL = [
    ("example.com", "domain", False),  # on the allow-list -> covered
    ("cloudflare.com", "domain", False),
    ("www.google.com", "domain", False),
    ("github.com", "domain", False),
    ("1.1.1.1", "ip", True),  # raw IP -> exploratory
]
_EDGE_VARIANTS = [  # exploratory: sidecar may canonicalize differently
    ("CLOUDFLARE.COM", "domain", True),
    ("cloudflare.com.", "domain", True),
    ("www.Google.com", "domain", True),
]
_FUZZ_DURATIONS = [
    DURATION_ONCE,
    DURATION_5S,  # test-only (#2363); exercises timed within/exceeding at run speed
    DURATION_5M,
    DURATION_1H,
    DURATION_TILRESTART,
    DURATION_FOREVER,
]
_FUZZ_ACTIONS = ["allow", "deny", "none"]  # 'none' = no response -> timeout
_DURATION_SECS = {DURATION_5S: 5, DURATION_5M: 300, DURATION_1H: 3600}


def _canonical(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _carries(duration: str) -> bool:
    # once (and the no-response expired) govern exactly one connection.
    return duration != DURATION_ONCE


@dataclass
class _Verdict:
    decision: str
    duration: str
    decided_at: float


class EgressModel:
    """Mirror of the sidecar's in-effect verdicts so the expected outcome for
    iteration N folds in verdicts from iterations <N on recurring dests."""

    def __init__(self, allow_list: list[str]) -> None:
        self.allow = {_canonical(d) for d in allow_list}
        self.verdicts: dict[str, _Verdict] = {}
        self.touched: set[str] = set()  # every host seen this run

    def _active(self, host: str, now: float) -> _Verdict | None:
        v = self.verdicts.get(host)
        if v is None:
            return None
        secs = _DURATION_SECS.get(v.duration)
        if secs is not None and now - v.decided_at > secs:
            return None
        return v

    def covers(
        self, host: str, now: float
    ) -> tuple[bool, str | None, str | None]:
        """(covered, decision, source); source is 'allowlist' / 'verdict' / None.

        The allow-list is a deterministic invariant; a verdict's carryover is
        best-effort (it races with CDN IP rotation + host canonicalization), so
        a caller should hard-fail only on allow-list coverage and treat verdict
        coverage as a softer signal.
        """
        if host in self.allow:
            return True, DECISION_ALLOWED, "allowlist"
        v = self._active(host, now)
        if v is not None:
            return True, v.decision, "verdict"
        return False, None, None

    def expect_request(self, host: str, now: float) -> bool:
        return not self.covers(host, now)[0]

    def record(
        self, host: str, decision: str, duration: str, now: float
    ) -> None:
        if not _carries(duration):
            return
        self.verdicts[host] = _Verdict(decision, duration, now)


@dataclass
class _Step:
    idx: int
    host: str
    kind: str
    exploratory: bool
    action: str
    duration: str


def gen_plan(seed: int, count: int) -> list[_Step]:
    """Deterministic fuzz plan, biased to repeat recent dests (carryover) and
    toward carrying durations (accumulating coverage)."""
    rng = random.Random(seed)
    steps: list[_Step] = []
    recent: list[str] = []
    # host -> (kind, exploratory) so a repeated dest keeps its flags.
    meta = {h: (k, e) for h, k, e in (*_POOL, *_EDGE_VARIANTS)}
    for i in range(count):
        roll = rng.random()
        if roll < 0.55:
            host, kind, expl = rng.choice([p for p in _POOL if not p[2]])
        elif roll < 0.75 and recent:
            host = rng.choice(recent)
            kind, expl = meta.get(host, ("domain", False))
        elif roll < 0.85:
            host, kind, expl = _POOL[-1]
        else:
            host, kind, expl = rng.choice(_EDGE_VARIANTS)
        recent.append(host)
        recent = recent[-6:]
        steps.append(
            _Step(
                i,
                host,
                kind,
                expl,
                rng.choice(_FUZZ_ACTIONS),
                rng.choice(_FUZZ_DURATIONS),
            )
        )
    return steps


# -- podman / container helpers ---------------------------------------------
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
    if not names:
        raise RuntimeError(f"no workspace container found for {ws_id}")
    return names[0]


def _trigger(container: str, host: str, outfile: str) -> None:
    # Detached curl: blocks on the held SYN until a verdict/timeout, then writes
    # EXIT:$?. -k skips cert validation so an IP/cert-mismatch still gives a
    # clean connect(0)/refuse(7) signal.
    subprocess.run(
        [
            "podman",
            "exec",
            "-d",
            container,
            "bash",
            "-c",
            "curl -sS -k --max-time 30 -o /dev/null "
            f"https://{shlex.quote(host)} > {outfile} 2>&1; "
            f'echo "EXIT:$?" >> {outfile}',
        ],
        check=True,
        timeout=15,
    )


def _read_outfile(container: str, outfile: str) -> str:
    r = subprocess.run(
        ["podman", "exec", container, "cat", outfile],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return r.stdout


async def _wait_result(
    container: str, outfile: str, timeout: float = 45
) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = _read_outfile(container, outfile)
        if "EXIT:" in last:
            return last
        await asyncio.sleep(0.5)
    return last


_EXIT_RE = re.compile(r"EXIT:(-?\d+)")


def _parse_exit(text: str) -> int | None:
    m = None
    for m in _EXIT_RE.finditer(text):
        pass  # last match (marker is appended last)
    return int(m.group(1)) if m else None


# -- decider-app helpers -----------------------------------------------------
async def _wait_connected(app: ConsentDeciderApp, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app._connected:
            await asyncio.sleep(1.5)  # let klangkd finish decider registration
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("consent-decide TUI did not connect")


def _pending_for(app: ConsentDeciderApp, canon: str) -> str | None:
    for req in app.controller.pending.values():
        if _canonical(req.dest_host or "") == canon:
            return req.id
    return None


async def _wait_for_request(app, canon, timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rid = _pending_for(app, canon)
        if rid is not None:
            return rid
        await asyncio.sleep(0.2)
    return None


async def _wait_no_request(app, canon, window=6.0):
    """Return None if no request arrives in window, else the request id."""
    deadline = time.time() + window
    while time.time() < deadline:
        rid = _pending_for(app, canon)
        if rid is not None:
            return rid
        await asyncio.sleep(0.3)
    return None


async def _wait_resolved(app, rid, timeout, settle=2.5):
    """Wait until rid leaves pending AND stays gone (verdict or auto-expire)."""
    deadline = time.time() + timeout
    absent_since = None
    while time.time() < deadline:
        if rid not in app.controller.pending:
            if absent_since is None:
                absent_since = time.time()
            if time.time() - absent_since >= settle:
                return True
        else:
            absent_since = None
        await asyncio.sleep(0.2)
    return False


class RawDecider:
    """Minimal second consent decider over the raw decider WS.

    The textual ``ConsentDeciderApp`` is decider #1 (the human path driven by
    the fuzz loop); this is a lightweight second connection for the
    multi-decider / snapshot-replay phases, speaking the same
    ``egress_request`` / ``egress_resolved`` / ``verdict`` frames.
    """

    def __init__(self, ws) -> None:
        self.ws = ws
        self.held: dict[str, str] = {}  # request_id -> canonical host

    async def _recv(self, timeout: float):
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        except Exception:
            return (None, None)
        try:
            msg = json.loads(raw)
        except Exception:
            return ("ignored", None)
        t = msg.get("type")
        if t == "egress_request":
            req = msg.get("request") or {}
            rid = req.get("id")
            if isinstance(rid, str):
                self.held[rid] = _canonical(str(req.get("dest_host") or ""))
            return ("added", rid)
        if t == "egress_resolved":
            rid = msg.get("request_id")
            if isinstance(rid, str):
                self.held.pop(rid, None)
            return ("resolved", rid)
        return (t, None)

    async def settle(self, timeout: float = 4.0) -> None:
        """Drain the connect-time snapshot + any in-flight frames."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            await self._recv(0.5)

    async def wait_for(self, canon: str, timeout: float = 12.0) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for rid, h in self.held.items():
                if h == canon:
                    return rid
            await self._recv(0.4)
        return None

    def has(self, canon: str) -> bool:
        return any(h == canon for h in self.held.values())

    async def verdict(self, rid: str, decision: str, duration: str) -> None:
        await self.ws.send(make_verdict(rid, decision, duration))

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass


# -- outcome classification --------------------------------------------------
def _classify_conn(expect: str, exit_code: int | None) -> str:
    if exit_code is None:
        return (
            FINDING  # curl never wrote EXIT (slow dest) -> not a consent bug
        )
    if expect == EXPECT_RELEASED:
        if exit_code == 0:
            return PASS
        if exit_code == EXIT_REFUSED:
            return MISMATCH  # an allow refused the connection
        return FINDING
    if expect == EXPECT_REFUSED:
        if exit_code == EXIT_REFUSED:
            return PASS
        if exit_code == 0:
            return MISMATCH  # a deny let the connection through
        return FINDING
    # EXPECT_NOT0
    return MISMATCH if exit_code == 0 else PASS


def _exit_label(exit_code: int | None) -> str:
    if exit_code is None:
        return "no-exit"
    tag = {
        0: "OK",
        EXIT_REFUSED: "REFUSED",
        6: "NORESOLVE",
        28: "TIMEOUT",
    }.get(exit_code)
    return f"exit{exit_code}" + (f":{tag}" if tag else "")


@dataclass
class _Result:
    step: _Step
    canon: str
    expect_request: bool
    expect_conn: str
    action_taken: str
    sidecar: str
    exit_code: int | None
    status: str
    detail: str = ""


@dataclass
class _Summary:
    total: int = 0
    passed: int = 0
    findings: int = 0
    mismatches: int = 0
    rows: list[_Result] = field(default_factory=list)


class SmokeTest:
    def __init__(self, args) -> None:
        self.args = args
        self.model = EgressModel(_ALLOW_LIST)
        self.summary = _Summary()
        self._owned_server: dict | None = None
        self.server: dict | None = None
        self.auth: dict | None = None
        self.ws_id: str | None = None
        self.ws_conn = None
        self._drain_task: asyncio.Task | None = None
        self.app: ConsentDeciderApp | None = None
        self.container: str | None = None
        self._shared_d2: RawDecider | None = None

    # -- setup ------------------------------------------------------------
    def _start_server(self) -> dict:
        try:
            return start_server(
                uds=False,
                KLANGKD_JWT_SECRET="smoketest-egress-secret",
                KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
                KLANGKD_DEFAULT_USER="smoke@example.com",
                KLANGKD_DEFAULT_PASSWORD="smokepass",
                KLANGKD_TEST_MODE="1",
                KLANGKD_ALLOW_AUTOSTART="1",
                KLANGKD_IDLE_TIMEOUT_SECONDS="3600",
                KLANGKD_EGRESS_CONSENT_TIMEOUT=str(self.args.consent_timeout),
                # Shorten the sidecar's learned-IP TTL floor + sweep cadence
                # (forwarded to the sidecar by klangkd) so a `5s` verdict really
                # expires in ~5s, not the 30s floor -- lets the run test a timed
                # verdict's within/exceeding in seconds (#2363).
                KLANGKNETWORK_EGRESS_MIN_TTL="1",
                KLANGKNETWORK_EGRESS_SWEEP_INTERVAL="1",
                LOGFIRE_TOKEN="",
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"\n!! could not start klangkd: {exc!r}\n\n"
                "Start one yourself, then point the smoketest at it:\n\n"
                "  devenv --quiet shell -- klangkd\n"
                "  devenv --quiet shell -- python "
                "src/klangk/klangkd-tests/e2e-tests/smoketest_egress.py "
                "--server http://localhost:<port>\n",
                file=sys.stderr,
            )
            raise SystemExit(2)

    @staticmethod
    def _login(url: str) -> dict:
        client = httpx.Client(base_url=url, timeout=30)
        try:
            r = client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "smoke@example.com",
                    "password": "smokepass",
                },
            )
            if r.status_code != 200:
                raise RuntimeError(f"login failed: {r.status_code} {r.text}")
            token = r.json()["access_token"]
        finally:
            client.close()
        return {
            "token": token,
            "headers": {"Authorization": f"Bearer {token}"},
        }

    @staticmethod
    def _create_workspace(server: dict, auth: dict) -> str:
        client = httpx.Client(
            base_url=server["url"], headers=auth["headers"], timeout=120
        )
        try:
            r = client.post(
                "/api/v1/workspaces",
                json={
                    "name": f"smoke-{int(time.time() * 1000) % 100000}",
                    "allowed_domains": _ALLOW_LIST,
                    "rejected_domains": _REJECTED_LIST,
                    "egress_mode": "interactive",
                    "auto_start": True,
                },
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"workspace create failed: {r.status_code} {r.text}"
                )
            return r.json()["id"]
        finally:
            client.close()

    @staticmethod
    def _create_static_workspace(server: dict, auth: dict) -> str:
        """A workspace with ``egress_mode='static'`` and no running container
        (auto_start=False). Used by the #2395 refusal check: such a workspace
        must refuse consent-decider registration. No container -> no sidecar."""
        client = httpx.Client(
            base_url=server["url"], headers=auth["headers"], timeout=120
        )
        try:
            r = client.post(
                "/api/v1/workspaces",
                json={
                    "name": f"smoke-static-{int(time.time() * 1000) % 100000}",
                    "egress_mode": "static",
                    "auto_start": False,
                },
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"static workspace create failed: {r.status_code} {r.text}"
                )
            return r.json()["id"]
        finally:
            client.close()

    async def _wait_container_ready(self) -> None:
        # Open the workspace terminal WS: confirms container_ready AND keeps
        # the workspace from idle-stopping during the run.
        ws = await ws_connect(self.server, f"/ws?token={self.auth['token']}")
        await ws.send(
            json.dumps({"cmd": "workspace_connect", "workspaceId": self.ws_id})
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            if msg.get("type") == "container_ready":
                self.ws_conn = ws
                self._drain_task = asyncio.create_task(self._drain(ws))
                return
        await ws.close()
        raise RuntimeError("container_ready not received within 120s")

    @staticmethod
    async def _drain(ws) -> None:
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    async def setup(self) -> None:
        if self.args.server:
            url = self.args.server.rstrip("/")
            self.server = {
                "url": url,
                "uds_path": None,
                "client": httpx.Client(base_url=url, timeout=120),
            }
            print(f"using external server: {url}")
        else:
            self._owned_server = self._start_server()
            self.server = self._owned_server
            print(f"started klangkd: {self.server['url']}")
        self.auth = await asyncio.to_thread(self._login, self.server["url"])
        self.ws_id = await asyncio.to_thread(
            self._create_workspace, self.server, self.auth
        )
        print(f"workspace {self.ws_id}  allow-list={_ALLOW_LIST}  interactive")
        await self._wait_container_ready()
        self.container = _container_for_workspace(self.ws_id)
        print(f"container: {self.container}")
        self.app = ConsentDeciderApp(
            server_url=self.server["url"],
            token=self.auth["token"],
            workspace_id=self.ws_id,
            workspace_name="smoketest-egress",
            hold_timeout=float(self.args.consent_timeout),
        )
        print(
            f"plan: count={self.args.count} seed={self.args.seed} "
            f"consent_timeout={self.args.consent_timeout}s "
            f"stop_on_mismatch={'no' if self.args.continue_run else 'yes'}\n"
        )

    # -- per-iteration -----------------------------------------------------
    async def _run_step(self, pilot, step: _Step) -> _Result:
        canon = _canonical(step.host)
        now = time.time()
        covered, cov_decision, _src = self.model.covers(canon, now)
        expect_request = not covered
        outfile = f"/tmp/smoke_{step.idx}.out"
        expl = step.exploratory

        if expect_request:
            expect_conn = {
                "allow": EXPECT_RELEASED,
                "deny": EXPECT_REFUSED,
                "none": EXPECT_NOT0,
            }[step.action]
        else:
            expect_conn = (
                EXPECT_RELEASED
                if cov_decision == DECISION_ALLOWED
                else EXPECT_REFUSED
            )

        _trigger(self.container, step.host, outfile)

        if expect_request:
            rid = await _wait_for_request(self.app, canon, timeout=12.0)
            if rid is None:
                # A hang (no exit) is an NFQUEUE/DNS hiccup -> finding; a clean
                # resolution with no prompt is a real mismatch (unless exploratory).
                text = await _wait_result(
                    self.container, outfile, timeout=10.0
                )
                ec = _parse_exit(text)
                if ec is None:
                    status = FINDING
                    detail = "expected a request; connection hung (NFQUEUE/DNS, not a consent failure)"
                else:
                    status = FINDING if expl else MISMATCH
                    detail = "expected a request, none arrived (connection resolved cleanly)"
                return self._record(
                    _Result(
                        step,
                        canon,
                        expect_request,
                        expect_conn,
                        step.action,
                        "no-request(!)",
                        ec,
                        status,
                        detail=detail,
                    )
                )
            action_taken = step.action
            if step.action == "none":
                # No verdict: the server auto-expires the held request at the
                # consent timeout (a one-shot deny) -> nothing carries over.
                resolved = await _wait_resolved(
                    self.app, rid, timeout=self.args.consent_timeout + 15
                )
                sidecar = "expired" if resolved else "held(!)"
            else:
                decision = (
                    DECISION_ALLOWED
                    if step.action == "allow"
                    else DECISION_DENIED
                )
                self.app._decide_id(rid, decision, step.duration)
                await pilot.pause()
                resolved = await _wait_resolved(self.app, rid, timeout=20.0)
                sidecar = "resolved" if resolved else "held(!)"
                # once is consumed by this connection; the rest cover the dest.
                self.model.record(canon, decision, step.duration, time.time())
            text = await _wait_result(self.container, outfile)
            exit_code = _parse_exit(text)
            status = _classify_conn(expect_conn, exit_code)
            res = self._record(
                _Result(
                    step,
                    canon,
                    expect_request,
                    expect_conn,
                    action_taken,
                    sidecar,
                    exit_code,
                    status,
                )
            )
            await self._retries_after_verdict(pilot, step, canon)
            return res
        # covered: expect NO request
        intruder = await _wait_no_request(self.app, canon, window=6.0)
        text = await _wait_result(self.container, outfile)
        exit_code = _parse_exit(text)
        if intruder is not None:
            status = FINDING if expl else MISMATCH
            return self._record(
                _Result(
                    step,
                    canon,
                    expect_request,
                    expect_conn,
                    "-",
                    "request(!)",
                    exit_code,
                    status,
                    detail=f"expected no request (covered:{cov_decision}), one arrived",
                )
            )
        status = _classify_conn(expect_conn, exit_code)
        return self._record(
            _Result(
                step,
                canon,
                expect_request,
                expect_conn,
                "-",
                "no-req",
                exit_code,
                status,
            )
        )

    def _record(self, res: _Result) -> _Result:
        self.summary.total += 1
        self.summary.rows.append(res)
        if res.status == PASS:
            self.summary.passed += 1
        elif res.status == FINDING:
            self.summary.findings += 1
        else:
            self.summary.mismatches += 1
        self._print_row(res)
        return res

    @staticmethod
    def _mark(status: str) -> str:
        return {PASS: "✓", MISMATCH: "✗ MISMATCH", FINDING: "⚠ finding"}[
            status
        ]

    def _print_row(self, r: _Result) -> None:
        cov = "" if r.expect_request else "(covered)"
        action = r.action_taken if r.expect_request else "-"
        dur = ""
        if r.expect_request and r.action_taken != "-":
            dur = (
                f"/{r.step.duration}"
                if r.action_taken != "none"
                else "(timeout)"
            )
            action = f"{r.action_taken}{dur}"
        print(
            f"[{r.step.idx + 1:>{len(str(self.args.count))}}/{self.args.count}] "
            f"dest={r.step.host:<18} {r.step.kind:<6} {cov:<9} "
            f"-> {action:<18} sidecar={r.sidecar:<12} conn={_exit_label(r.exit_code):<14} "
            f"{self._mark(r.status)}"
        )
        if r.detail:
            print(f"       ... {r.detail}")

    # -- retries: within / exceeding a duration (#2392) -------------------
    async def _retries_after_verdict(
        self, pilot, step: _Step, canon: str
    ) -> None:
        """After issuing a verdict, reconnect to probe the duration's effect.

        - ``once``: consumed by the deciding connection, so an immediate retry
          must re-prompt (it has already *exceeded* the once lifetime).
        - any carrying duration: an immediate retry stays in effect (it is
          *within* the window) -> no re-prompt, same enforcement.
        Timed-duration *expiry* (exceeding a timed window) is exercised by the
        dedicated lifecycle phase (:meth:`run_duration_lifecycle`) and by fuzz
        recurrence after the window elapses.
        """
        if not self.args.retries:
            return
        # 'none' (timeout) is its own test (the consent-timeout auto-deny); a
        # retry right after it is confounded by the short fail-close REJECT the
        # sidecar installs for a timeout (CONSENT_REJECT_TTL), so skip it.
        if step.action == "none":
            return
        # 'once' carries nothing -> a retry must re-prompt (already exceeded).
        if step.duration == DURATION_ONCE:
            await self._probe(
                pilot,
                step,
                canon,
                "exceeding-retry(once)",
                expect_request=True,
                expect_conn=EXPECT_NOT0,
            )
            return
        # Give the sidecar a beat to install the learned ACCEPT/REJECT rule
        # (it runs in an executor off the NFQUEUE consumer) before reconnecting.
        await asyncio.sleep(1.5)
        within_conn = (
            EXPECT_RELEASED if step.action == "allow" else EXPECT_REFUSED
        )
        await self._probe(
            pilot,
            step,
            canon,
            "within-retry",
            expect_request=False,
            expect_conn=within_conn,
        )

    async def _probe(
        self,
        pilot,
        parent: _Step,
        canon: str,
        label: str,
        expect_request: bool,
        expect_conn: str,
    ) -> str:
        """One reconnect probe; records an indented sub-line. Returns status."""
        outfile = f"/tmp/smoke_p_{self.summary.total}.out"
        _trigger(self.container, parent.host, outfile)
        if expect_request:
            rid = await _wait_for_request(self.app, canon, timeout=12.0)
            if rid is None:
                # Did the connection hang, or resolve cleanly without a prompt?
                # A hang (no exit) is an NFQUEUE/DNS hiccup -> finding, not a
                # consent-semantics failure; a clean resolution with no prompt
                # means the verdict never lapsed -> real mismatch.
                text = await _wait_result(
                    self.container, outfile, timeout=10.0
                )
                ec = _parse_exit(text)
                if ec is None:
                    return self._record_probe(
                        parent,
                        label,
                        "no-request(!)",
                        None,
                        FINDING,
                        "expected a re-prompt; connection hung "
                        "(NFQUEUE/DNS, not a consent failure)",
                    )
                status = FINDING if parent.exploratory else MISMATCH
                return self._record_probe(
                    parent,
                    label,
                    "no-request(!)",
                    ec,
                    status,
                    "expected a re-prompt; none arrived but the "
                    "connection resolved cleanly",
                )
            # settle so the held request doesn't dangle: allow/once releases
            # just this SYN (ttl None -> no learn, no REJECT) so it can't
            # pollute a later probe's expectations.
            self.app._decide_id(rid, DECISION_ALLOWED, DURATION_ONCE)
            await pilot.pause()
            await _wait_resolved(self.app, rid, timeout=15.0)
            text = await _wait_result(self.container, outfile)
            return self._record_probe(
                parent,
                label,
                "re-prompt",
                _parse_exit(text),
                PASS,
                "request arrived as expected",
            )
        # expect no request (verdict in effect)
        intruder = await _wait_no_request(self.app, canon, window=6.0)
        text = await _wait_result(self.container, outfile)
        exit_code = _parse_exit(text)
        if intruder is not None:
            return self._record_probe(
                parent,
                label,
                "request(!)",
                exit_code,
                FINDING,
                "verdict not in effect (CDN/carryover surprise)",
            )
        status = _classify_conn(expect_conn, exit_code)
        return self._record_probe(
            parent, label, "no-req", exit_code, status, ""
        )

    def _record_probe(
        self,
        parent: _Step,
        label: str,
        sidecar: str,
        exit_code: int | None,
        status: str,
        detail: str,
    ) -> str:
        self.summary.total += 1
        if status == PASS:
            self.summary.passed += 1
        elif status == FINDING:
            self.summary.findings += 1
        else:
            self.summary.mismatches += 1
        self.summary.rows.append(
            _Result(
                parent,
                _canonical(parent.host),
                True,
                "",
                label,
                sidecar,
                exit_code,
                status,
                detail=detail,
            )
        )
        indent = " " * (len(str(self.args.count)) + 3)
        print(
            f"{indent}↳ {label:<20} sidecar={sidecar:<12} "
            f"conn={_exit_label(exit_code):<14} {self._mark(status)}"
        )
        if detail:
            print(f"{indent}   ... {detail}")
        if status == MISMATCH and not self.args.continue_run:
            self._abort = True
        return status

    async def run_duration_lifecycle(self, pilot) -> None:
        """Deterministic within/exceeding demonstration using the 5s duration.

        For an allow and a deny: decide 5s -> within-retry (verdict in effect)
        -> wait past the 5s window -> exceeding-retry (verdict expired, fresh
        re-prompt). The within-retry is best-effort (CDN IP rotation can
        re-prompt a freshly-learned IP -> finding); the exceeding-retry is the
        deterministic expiry check.
        """
        if not self.args.lifecycle:
            return
        print("\n--- duration lifecycle: within vs exceeding a 5s verdict ---")
        cases = [
            ("www.example.org", DECISION_ALLOWED, "allow", EXPECT_RELEASED),
            ("api.github.com", DECISION_DENIED, "deny", EXPECT_REFUSED),
        ]
        for host, decision, label, main_conn in cases:
            if self._abort:
                return
            canon = _canonical(host)
            step = _Step(
                self.summary.total, host, "domain", True, label, DURATION_5S
            )
            outfile = f"/tmp/smoke_life_{self.summary.total}.out"
            _trigger(self.container, host, outfile)
            rid = await _wait_for_request(self.app, canon, timeout=12.0)
            if rid is None:
                self._record_probe(
                    step,
                    f"{label}/5s decide",
                    "no-request(!)",
                    None,
                    MISMATCH,
                    "no request for a fresh off-list host",
                )
                continue
            self.app._decide_id(rid, decision, DURATION_5S)
            await pilot.pause()
            await _wait_resolved(self.app, rid, timeout=20.0)
            text = await _wait_result(self.container, outfile)
            ec = _parse_exit(text)
            self._record_probe(
                step,
                f"{label}/5s decide",
                "resolved",
                ec,
                _classify_conn(main_conn, ec),
                "",
            )
            # within: immediate retry, verdict still in effect.
            pr = await self._probe(
                pilot,
                step,
                canon,
                "within-retry",
                expect_request=False,
                expect_conn=main_conn,
            )
            if pr == MISMATCH and not self.args.continue_run:
                return
            # wait past the 5s window (sidecar TTL=5, sweep=1 -> gone by ~6s).
            await asyncio.sleep(self.args.expiry_wait)
            # exceeding: verdict expired -> a fresh re-prompt.
            await self._probe(
                pilot,
                step,
                canon,
                "exceeding-retry",
                expect_request=True,
                expect_conn=EXPECT_NOT0,
            )

    async def _connect_raw_decider(self, attempts: int = 2) -> RawDecider:
        # Retry once with a generous open timeout as a safety net against a
        # transient accept hiccup; bounded so a persistent issue fails fast
        # (the caller records a finding) rather than hanging the run.
        # (Was framed as a TCP-proxy flakiness workaround; that premise was
        # disproven — the proxy is reliable at this concurrency, #2398.)
        url = (
            f"/ws/consent-decider?token={self.auth['token']}"
            f"&workspace={self.ws_id}"
        )
        last: Exception | None = None
        for _ in range(attempts):
            try:
                ws = await ws_connect(self.server, url, open_timeout=15)
                return RawDecider(ws)
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(1.0)
        assert last is not None
        raise last

    async def _wait_app_connected(self, want: bool, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.app._connected == want:
                return True
            await asyncio.sleep(0.2)
        return False

    async def _get_shared_decider(self) -> RawDecider:
        """One raw second decider shared across the multi-decider + snapshot
        phases — one extra decider connection rather than opening/closing
        one per phase."""
        if self._shared_d2 is None:
            self._shared_d2 = await self._connect_raw_decider()
            await self._shared_d2.settle()
        return self._shared_d2

    async def run_multi_decider_phase(self, pilot) -> None:
        """Multiple deciders: a 2nd decider's verdict resolves a hold and the
        textual app sees it (co-decider sync); and a verdict from decider #2
        wins, so the app's later verdict on the same request is a no-op
        (first-decision-wins)."""
        if not self.args.multi_decider:
            return
        print(
            "\n--- multiple deciders: first-decision-wins + co-decider sync ---"
        )
        try:
            d2 = await self._get_shared_decider()
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                _Step(
                    self.summary.total,
                    "(decider2)",
                    "n/a",
                    False,
                    "multi-decider",
                    "-",
                ),
                "attach decider2",
                "error",
                None,
                MISMATCH,
                f"could not attach a 2nd decider: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
            return
        # 1) co-decider sync: decider2 denies a held request; the app drops it.
        host_x = "stackoverflow.com"
        cx = _canonical(host_x)
        step_x = _Step(
            self.summary.total, host_x, "domain", False, "multi-decider", "-"
        )
        of_x = f"/tmp/smoke_md_{self.summary.total}.out"
        _trigger(self.container, host_x, of_x)
        rx = await d2.wait_for(cx, 12.0)
        app_rx = await _wait_for_request(self.app, cx, 8.0) if rx else None
        if not rx or not app_rx:
            self._record_probe(
                step_x,
                "hold X",
                "no-request(!)",
                None,
                MISMATCH,
                "X not seen by BOTH deciders",
            )
            await d2.close()
            if not self.args.continue_run:
                self._abort = True
            return
        await d2.verdict(rx, DECISION_DENIED, DURATION_ONCE)
        synced = await _wait_resolved(self.app, rx, 15.0)
        text_x = await _wait_result(self.container, of_x)
        ecx = _parse_exit(text_x)
        if not synced:
            sx, dx = (
                FINDING,
                "app didn't see decider2's resolve (sidecar hiccup)",
            )
        elif ecx == EXIT_REFUSED:
            sx, dx = PASS, ""
        elif ecx == 0:
            sx, dx = MISMATCH, "decider2's deny let the connection through"
        else:
            sx, dx = FINDING, f"connection not cleanly refused (exit {ecx})"
        self._record_probe(
            step_x,
            "decider2 denies -> app synced",
            "resolved" if synced else "held(!)",
            ecx,
            sx,
            dx,
        )
        if sx == MISMATCH and not self.args.continue_run:
            self._abort = True
            return
        # 2) first-decision-wins: the SAME decider2 denies Y first; the app's
        #    late allow must be a no-op (first verdict wins -> refused).
        host_y = "reddit.com"
        cy = _canonical(host_y)
        step_y = _Step(
            self.summary.total, host_y, "domain", False, "multi-decider", "-"
        )
        of_y = f"/tmp/smoke_md_{self.summary.total}.out"
        _trigger(self.container, host_y, of_y)
        ry = await d2.wait_for(cy, 12.0)
        if not ry:
            self._record_probe(
                step_y, "hold Y", "no-request(!)", None, MISMATCH, "Y not held"
            )
            await d2.close()
            if not self.args.continue_run:
                self._abort = True
            return
        await d2.verdict(
            ry, DECISION_DENIED, DURATION_ONCE
        )  # first -> deny wins
        await asyncio.sleep(0.5)
        self.app._decide_id(
            ry, DECISION_ALLOWED, DURATION_ONCE
        )  # late -> no-op
        await pilot.pause()
        await _wait_resolved(self.app, ry, 15.0)
        text_y = await _wait_result(self.container, of_y)
        ecy = _parse_exit(text_y)
        if ecy == EXIT_REFUSED:
            sy, dy = PASS, ""
        elif ecy == 0:
            sy, dy = (
                MISMATCH,
                "late allow let it through (first-decision-wins violation)",
            )
        else:
            sy, dy = FINDING, f"connection not cleanly refused (exit {ecy})"
        self._record_probe(
            step_y,
            "first(deny) wins, late allow no-op",
            "resolved",
            ecy,
            sy,
            dy,
        )
        if sy == MISMATCH and not self.args.continue_run:
            self._abort = True

    async def run_snapshot_replay_phase(self, pilot) -> None:
        """Reconnect snapshot replay: a request resolved while a decider is
        disconnected does NOT replay in the snapshot it gets on reconnect; a
        still-held one does."""
        if not self.args.snapshot:
            return
        print(
            "\n--- reconnect snapshot replay: resolved-while-away rows don't replay ---"
        )
        step = _Step(
            self.summary.total, "snapshot", "n/a", False, "snapshot", "-"
        )
        try:
            d2 = await self._get_shared_decider()
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                step,
                "attach decider2",
                "error",
                None,
                MISMATCH,
                f"could not attach a 2nd decider: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
            return
        host_a, host_b = "amazon.com", "microsoft.com"
        ca, cb = _canonical(host_a), _canonical(host_b)
        of_a, of_b = "/tmp/smoke_sr_a.out", "/tmp/smoke_sr_b.out"
        _trigger(self.container, host_a, of_a)
        _trigger(self.container, host_b, of_b)
        ra = await d2.wait_for(ca, 12.0)
        rb = await d2.wait_for(cb, 12.0)
        if not ra or not rb:
            for rid in (ra, rb):
                if rid:
                    self.app._decide_id(rid, DECISION_DENIED, DURATION_ONCE)
            await d2.close()
            text_a = await _wait_result(self.container, of_a, timeout=8.0)
            ec = _parse_exit(text_a)
            if ec is None:
                ec = _parse_exit(
                    await _wait_result(self.container, of_b, timeout=4.0)
                )
            if ec is None:
                self._record_probe(
                    step,
                    "hold A+B",
                    "no-request(!)",
                    None,
                    FINDING,
                    "A/B hung (NFQUEUE/DNS; possibly a concurrent-hold issue)",
                )
            else:
                self._record_probe(
                    step,
                    "hold A+B",
                    "no-request(!)",
                    ec,
                    MISMATCH,
                    f"A/B not both held (resolved w/o prompt, exit {ec})",
                )
                if not self.args.continue_run:
                    self._abort = True
            return
        # The APP is the reconnecting decider; d2 (still connected) resolves A
        # while the app is away. B stays held. (Keeping a single raw decider
        # connected avoids the proxy's flaky 2nd-handshake behavior.)
        self.app.reconnect_delays = (4.0,)  # widen the window to resolve A in
        try:
            if self.app._ws is not None:
                await self.app._ws.close()
        except Exception:
            pass
        await self._wait_app_connected(False, 5.0)
        await d2.verdict(ra, DECISION_DENIED, DURATION_ONCE)  # resolve A away
        await asyncio.sleep(1.0)  # let the resolve land server-side
        await self._wait_app_connected(True, 12.0)
        await asyncio.sleep(1.5)  # let the reconnect snapshot frames apply
        has_b = any(
            _canonical(r.dest_host or "") == cb
            for r in self.app.controller.pending.values()
        )
        has_a = any(
            _canonical(r.dest_host or "") == ca
            for r in self.app.controller.pending.values()
        )
        ok = has_b and not has_a
        if ok:
            status, detail = (
                PASS,
                "snapshot has the still-held B, not the resolved A",
            )
        elif has_b and has_a:
            # A denied on one IP -> curl tries the next IP (CDN cascade) -> a NEW
            # held request for A appears; can't cleanly distinguish from a replay
            # bug with real multi-IP hosts -> finding, not a failure.
            status, detail = (
                FINDING,
                "A present (likely a CDN-cascade respawn, not a replay bug)",
            )
        else:
            status, detail = (
                FINDING,
                f"B missing from snapshot (timing/cascade): B={has_b} A={has_a}",
            )
        self._record_probe(
            step,
            "reconnect snapshot",
            f"B={'y' if has_b else 'n'},A={'y' if has_a else 'n'}",
            None,
            status,
            detail,
        )
        # Clean up B so it doesn't dangle (bounded -- cascade curls can linger).
        self.app._decide_id(rb, DECISION_DENIED, DURATION_ONCE)
        await pilot.pause()
        await _wait_resolved(self.app, rb, 15.0)
        await _wait_result(self.container, of_a, timeout=10.0)
        await _wait_result(self.container, of_b, timeout=10.0)

    async def run_static_refusal_phase(self) -> None:
        """#2395: a workspace with ``egress_mode='static'`` must refuse
        consent-decider registration (a 4003 close), so a decider can't flip a
        static workspace interactive merely by connecting."""
        if not self.args.static_refusal:
            return
        print(
            "\n--- #2395: a static workspace refuses consent-decider "
            "registration ---"
        )
        step = _Step(
            self.summary.total,
            "(static ws)",
            "n/a",
            False,
            "static-refusal",
            "-",
        )
        static_ws = await asyncio.to_thread(
            self._create_static_workspace, self.server, self.auth
        )
        try:
            url = (
                f"/ws/consent-decider?token={self.auth['token']}"
                f"&workspace={static_ws}"
            )
            try:
                ws = await ws_connect(self.server, url, open_timeout=15)
            except Exception as e:  # noqa: BLE001
                self._record_probe(
                    step,
                    "decider->static",
                    "refused(handshake)",
                    None,
                    PASS,
                    f"registration refused at the handshake: {e!r}",
                )
                return
            try:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5.0)
                    # A frame (the snapshot) arrived -> the decider registered
                    # against a static workspace (#2395 violation).
                    self._record_probe(
                        step,
                        "decider->static",
                        "attached(!)",
                        None,
                        MISMATCH,
                        "a decider registered against a static workspace",
                    )
                except Exception as e:  # noqa: BLE001  (ConnectionClosed / timeout)
                    if "Closed" in type(e).__name__:
                        code = getattr(e, "code", None)
                        self._record_probe(
                            step,
                            "decider->static",
                            f"refused({code})",
                            None,
                            PASS,
                            "registration refused (connection closed)",
                        )
                    else:
                        self._record_probe(
                            step,
                            "decider->static",
                            "no-close",
                            None,
                            FINDING,
                            f"neither a frame nor a close: {e!r}",
                        )
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
        finally:
            await asyncio.to_thread(
                self._delete_workspace, self.server, self.auth, static_ws
            )

    async def run_rejected_phase(self, pilot) -> None:
        """#2367: a rejected_domains host is pre-emptively denied at the sidecar
        -- no consent request is ever surfaced, even with a decider attached
        (interactive mode). Contrasted with a normal off-list host, which IS
        held for consent. Driven over the app's own decider (proxy-independent)."""
        if not self.args.rejected:
            return
        print(
            "\n--- rejected_domains: pre-emptive deny (no consent prompt) ---"
        )
        # 1) the rejected host: denied with NO prompt.
        rh = "kernel.org"
        crh = _canonical(rh)
        step_r = _Step(
            self.summary.total, rh, "domain", False, "rejected", "-"
        )
        of_r = f"/tmp/smoke_rj_{self.summary.total}.out"
        _trigger(self.container, rh, of_r)
        intruder = await _wait_no_request(self.app, crh, window=6.0)
        text_r = await _wait_result(self.container, of_r, timeout=15.0)
        ec_r = _parse_exit(text_r)
        if intruder is not None:
            status = MISMATCH
            detail = "a rejected host prompted for consent (not pre-empted)"
            self.app._decide_id(intruder, DECISION_DENIED, DURATION_ONCE)
            await pilot.pause()
            await _wait_resolved(self.app, intruder, timeout=15.0)
            sidecar = "request(!)"
        elif ec_r is None:
            status, detail, sidecar = (
                FINDING,
                "rejected host hung (NFQUEUE/DNS)",
                "held(!)",
            )
        elif ec_r == 0:
            status, detail, sidecar = (
                MISMATCH,
                "rejected host succeeded (leak)",
                "leak(!)",
            )
        else:
            status, detail, sidecar = (
                PASS,
                f"pre-emptively denied (exit {ec_r}), no prompt",
                "denied",
            )
        self._record_probe(
            step_r, "rejected host", sidecar, ec_r, status, detail
        )
        if status == MISMATCH and not self.args.continue_run:
            self._abort = True
            return
        # 2) contrast: a normal off-list host IS held for consent.
        nh = "ubuntu.com"
        cnh = _canonical(nh)
        step_n = _Step(
            self.summary.total, nh, "domain", False, "rejected", "-"
        )
        of_n = f"/tmp/smoke_rn_{self.summary.total}.out"
        _trigger(self.container, nh, of_n)
        rid = await _wait_for_request(self.app, cnh, timeout=12.0)
        if rid is not None:
            self.app._decide_id(rid, DECISION_DENIED, DURATION_ONCE)
            await pilot.pause()
            await _wait_resolved(self.app, rid, timeout=15.0)
            ec_n = _parse_exit(await _wait_result(self.container, of_n))
            self._record_probe(
                step_n,
                "off-list contrast",
                "prompted",
                ec_n,
                PASS,
                "off-list host prompted (contrast: rejected did not)",
            )
        else:
            ec_n = _parse_exit(
                await _wait_result(self.container, of_n, timeout=10.0)
            )
            self._record_probe(
                step_n,
                "off-list contrast",
                "no-request(!)",
                ec_n,
                FINDING if ec_n is None else FINDING,
                "off-list host did not prompt (env?)",
            )

    async def run_revoke_phase(self, pilot) -> None:
        """#2339/#2396: revoking an in-effect verdict drops its rule, so the next
        connection to that host re-prompts. Uses a deny (its within-retry
        reliably holds, unlike an allow per #2399) so 'covered' is provable
        before the revoke. Driven over the app's own decider WS (no 2nd WS)."""
        if not self.args.revoke:
            return
        print(
            "\n--- revoke: a dropped verdict re-prompts the next connection ---"
        )
        host = "aws.amazon.com"  # fresh: untouched by any other phase
        canon = _canonical(host)
        step = _Step(self.summary.total, host, "domain", False, "revoke", "-")
        of1 = f"/tmp/smoke_rv_{self.summary.total}.out"
        _trigger(self.container, host, of1)
        rid = await _wait_for_request(self.app, canon, timeout=12.0)
        if rid is None:
            text = await _wait_result(self.container, of1, timeout=10.0)
            ec = _parse_exit(text)
            self._record_probe(
                step,
                "hold",
                "no-request(!)",
                ec,
                FINDING if ec is None else MISMATCH,
                "off-list host was not held for consent",
            )
            if ec is not None and not self.args.continue_run:
                self._abort = True
            return
        # Deny/tilrestart -> covered (a deny persists across a retry).
        self.app._decide_id(rid, DECISION_DENIED, DURATION_TILRESTART)
        await pilot.pause()
        await _wait_resolved(self.app, rid, timeout=15.0)
        pr = await self._probe(
            pilot,
            step,
            canon,
            "deny within-retry",
            expect_request=False,
            expect_conn=EXPECT_REFUSED,
        )
        if pr == MISMATCH and not self.args.continue_run:
            self._abort = True
            return
        # Revoke the verdict -> the server drops the rule (sidecar REJECT gone).
        try:
            if self.app._ws is not None:
                await self.app._ws.send(make_revoke(rid))
        except Exception:
            pass
        await pilot.pause()
        await asyncio.sleep(2.0)  # let the server + sidecar process the drop
        # The next connection must re-prompt (the deny no longer covers it).
        of2 = f"/tmp/smoke_rv2_{self.summary.total}.out"
        _trigger(self.container, host, of2)
        rid2 = await _wait_for_request(self.app, canon, timeout=12.0)
        if rid2 is not None:
            self.app._decide_id(rid2, DECISION_DENIED, DURATION_ONCE)
            await pilot.pause()
            await _wait_resolved(self.app, rid2, timeout=15.0)
            ec2 = _parse_exit(await _wait_result(self.container, of2))
            self._record_probe(
                step,
                "revoke->re-prompt",
                "re-prompt",
                ec2,
                PASS,
                "deny revoked -> fresh re-prompt",
            )
        else:
            ec2 = _parse_exit(
                await _wait_result(self.container, of2, timeout=10.0)
            )
            if ec2 is None:
                status = FINDING
                detail = "post-revoke connection hung (NFQUEUE/DNS)"
            else:
                status = MISMATCH
                detail = (
                    f"no re-prompt after revoke (rule not dropped, exit {ec2})"
                )
            self._record_probe(
                step,
                "revoke->re-prompt",
                "no-reprompt(!)",
                ec2,
                status,
                detail,
            )
            if status == MISMATCH and not self.args.continue_run:
                self._abort = True

    async def run_fail_closed_phase(self, pilot) -> None:
        """#2308 fail-closed guarantee: with a request held pending, the decider
        disconnects -> the hold auto-denies on the consent timeout and the
        connection does NOT succeed (never silently allowed).

        ``pilot`` is unused (kept for signature symmetry with the other phases).
        """
        if not self.args.fail_closed:
            return
        print(
            "\n--- decider disconnects mid-hold: connection must fail (fail-closed) ---"
        )
        host = "en.wikipedia.org"  # fresh: never touched by another phase
        canon = _canonical(host)
        step = _Step(
            self.summary.total, host, "domain", False, "fail-closed", "-"
        )
        outfile = f"/tmp/smoke_fc_{self.summary.total}.out"
        _trigger(self.container, host, outfile)
        rid = await _wait_for_request(self.app, canon, timeout=12.0)
        if rid is None:
            text = await _wait_result(self.container, outfile, timeout=10.0)
            ec = _parse_exit(text)
            status = FINDING if ec is None else MISMATCH
            self._record_probe(
                step,
                "hold",
                "no-request(!)",
                ec,
                status,
                "off-list host was not held for consent",
            )
            if status == MISMATCH and not self.args.continue_run:
                self._abort = True
            return
        # Held pending. Disconnect the decider: stop the reconnect loop and
        # close the WS so the server deregisters it -> with no decider the
        # in-flight hold auto-denies on its own consent timeout (fail-closed).
        self.app._stop = True
        try:
            if self.app._ws is not None:
                await self.app._ws.close()
        except Exception:
            pass
        await pilot.pause()
        # The held SYN is released as a deny (forged RST) at the consent timeout.
        text = await _wait_result(
            self.container, outfile, timeout=self.args.consent_timeout + 15
        )
        ec = _parse_exit(text)
        if ec is None:
            status = FINDING
            detail = "connection hung after disconnect (NFQUEUE/DNS, not clean fail-closed)"
            sidecar = "held(!)"
        elif ec == 0:
            status = MISMATCH
            detail = (
                "connection SUCCEEDED after the decider disconnected "
                "(silent allow -- #2308 violation!)"
            )
            sidecar = "leak(!)"
        else:
            status = PASS
            detail = f"connection failed after disconnect (exit {ec}) -- fail-closed"
            sidecar = "auto-denied"
        self._record_probe(
            step, "disconnect->auto-deny", sidecar, ec, status, detail
        )
        if status == MISMATCH and not self.args.continue_run:
            self._abort = True

    async def run_no_decider_phase(self, pilot) -> None:
        """Static-mode phase: with NO consent decider registered, off-list egress
        is denied cleanly (no held request, no hang) while allow-list egress
        still connects -- the #2308 'reverts to static allow-list' guarantee.

        ``pilot`` is unused (kept for signature symmetry); this phase runs after
        the decider app has shut down, so there is no decider WS to drive.
        """
        if not self.args.static_phase:
            return
        print(
            "\n--- no decider registered: off-list must be denied (static mode) ---"
        )
        # run_test has exited -> the decider WS dropped -> the server deregistered
        # the decider -> the workspace reverted to static allow-list. Give the
        # server a beat to process the disconnect before probing.
        await asyncio.sleep(3.0)
        cases = [
            ("example.com", True, "domain"),  # allow-list apex -> connects
            ("duckduckgo.com", False, "domain"),  # FRESH off-list -> denied
            ("news.ycombinator.com", False, "domain"),
        ]
        # NOTE: bare-host = exact (#2377) can't be asserted here with real
        # domains -- a subdomain (e.g. www.example.com) shares the apex's IP,
        # and the sidecar's allow rule is L3/L4 (per IP), so the subdomain is
        # allowed regardless of the hostname spec. Asserting it needs controlled
        # DNS (the fake-upstream pattern), out of scope for this real-domain run.
        for host, allowed, kind in cases:
            if self._abort:
                return
            step = _Step(self.summary.total, host, kind, False, "static", "-")
            outfile = f"/tmp/smoke_nd_{self.summary.total}.out"
            _trigger(self.container, host, outfile)
            # A static denial is clean + prompt; cap well under curl's max-time
            # so a held/hung connection shows up as no-exit (a real regression).
            text = await _wait_result(self.container, outfile, timeout=20.0)
            exit_code = _parse_exit(text)
            if allowed:
                status = PASS if exit_code == 0 else MISMATCH
                self._record_probe(
                    step,
                    "static allow-list",
                    "allowed",
                    exit_code,
                    status,
                    "allow-list host should connect",
                )
            elif exit_code is None:
                self._record_probe(
                    step,
                    "static off-list",
                    "held(!)",
                    None,
                    MISMATCH,
                    "off-list hung (not a clean static denial)",
                )
            elif exit_code == 0:
                self._record_probe(
                    step,
                    "static off-list",
                    "leak(!)",
                    exit_code,
                    MISMATCH,
                    "off-list succeeded with no decider (leak)",
                )
            else:
                self._record_probe(
                    step,
                    "static off-list",
                    "denied",
                    exit_code,
                    PASS,
                    f"denied with no decider (exit {exit_code})",
                )

    # -- run / teardown ----------------------------------------------------
    async def run(self) -> int:
        await self.setup()
        plan = gen_plan(self.args.seed, self.args.count)
        self._abort = False
        stop = False
        try:
            if self.args.static_refusal:
                await self.run_static_refusal_phase()
            async with self.app.run_test() as pilot:
                await _wait_connected(self.app)
                for step in plan:
                    try:
                        res = await self._run_step(pilot, step)
                    except Exception as exc:  # noqa: BLE001
                        res = _Result(
                            step,
                            _canonical(step.host),
                            True,
                            "",
                            "?",
                            "error",
                            None,
                            MISMATCH,
                            detail=f"{exc!r}",
                        )
                        self.summary.total += 1
                        self.summary.mismatches += 1
                        self.summary.rows.append(res)
                        self._print_row(res)
                    if (
                        res.status == MISMATCH or self._abort
                    ) and not self.args.continue_run:
                        stop = True
                        break
                    await pilot.pause()
                if not stop and not self._abort:
                    await self.run_duration_lifecycle(pilot)
                if not stop and not self._abort:
                    await self.run_multi_decider_phase(pilot)
                if not stop and not self._abort:
                    await self.run_snapshot_replay_phase(pilot)
                if not stop and not self._abort:
                    await self.run_revoke_phase(pilot)
                if not stop and not self._abort:
                    await self.run_rejected_phase(pilot)
                if not stop and not self._abort:
                    await self.run_fail_closed_phase(pilot)
            # run_test exited -> the decider WS dropped -> the server deregistered
            # the decider -> the workspace reverted to static allow-list (#2308).
            if not stop and not self._abort and self.args.static_phase:
                await self.run_no_decider_phase(pilot)
        finally:
            await self.teardown()
        self._print_summary(stop)
        return 1 if self.summary.mismatches else 0

    async def teardown(self) -> None:
        if self._shared_d2 is not None:
            await self._shared_d2.close()
            self._shared_d2 = None
        if self.app is not None:
            try:
                self.app._stop = True
                self.app.exit()
            except Exception:
                pass
        if self.ws_conn is not None:
            try:
                await self.ws_conn.close()
            except Exception:
                pass
        if self._drain_task is not None:
            self._drain_task.cancel()
        if (
            self.ws_id
            and self.server
            and self.auth
            and self.args.delete_workspace
        ):
            try:
                await asyncio.to_thread(
                    self._delete_workspace, self.server, self.auth, self.ws_id
                )
            except Exception:
                pass
        if self._owned_server is not None:
            stop_server(self._owned_server)

    @staticmethod
    def _delete_workspace(server, auth, ws_id) -> None:
        client = httpx.Client(
            base_url=server["url"], headers=auth["headers"], timeout=60
        )
        try:
            client.delete(f"/api/v1/workspaces/{ws_id}")
        finally:
            client.close()

    def _print_summary(self, stopped: bool) -> None:
        s = self.summary
        print("\n" + "=" * 72)
        if stopped:
            print("STOPPED on first mismatch (--continue to run all).")
        print(
            f"iterations: {s.total}   pass: {s.passed}   "
            f"findings: {s.findings}   mismatches: {s.mismatches}"
        )
        findings = [r for r in s.rows if r.status == FINDING]
        if findings:
            print("\nfindings (under-specified; not failures):")
            for r in findings:
                print(
                    f"  [{r.step.idx + 1}] {r.step.host} ({r.step.kind}) "
                    f"action={r.action_taken} sidecar={r.sidecar} "
                    f"conn={_exit_label(r.exit_code)}  {r.detail}"
                )
        mism = [r for r in s.rows if r.status == MISMATCH]
        if mism:
            print("\nmismatches:")
            for r in mism:
                print(
                    f"  [{r.step.idx + 1}] {r.step.host} ({r.step.kind}) "
                    f"expect_req={r.expect_request} expect_conn={r.expect_conn} "
                    f"action={r.action_taken} sidecar={r.sidecar} "
                    f"conn={_exit_label(r.exit_code)}  {r.detail}"
                )
        print("=" * 72)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Interactive-egress fuzz smoketest (#2392)"
    )
    p.add_argument(
        "--count", type=int, default=50, help="iterations (default 50)"
    )
    p.add_argument(
        "--seed", type=int, default=None, help="PRNG seed (default: random)"
    )
    p.add_argument(
        "--consent-timeout",
        type=int,
        default=10,
        help="KLANGKD_EGRESS_CONSENT_TIMEOUT seconds (default 10)",
    )
    p.add_argument(
        "--server",
        default=None,
        help="use an existing klangkd at this URL instead of starting one",
    )
    p.add_argument(
        "--continue",
        dest="continue_run",
        action="store_true",
        help="keep going past a mismatch; summarize at the end",
    )
    p.add_argument(
        "--no-retries",
        dest="retries",
        action="store_false",
        help="disable within/exceeding reconnect retries after a verdict",
    )
    p.add_argument(
        "--no-lifecycle",
        dest="lifecycle",
        action="store_false",
        help="skip the deterministic 5s within/exceeding lifecycle phase",
    )
    p.add_argument(
        "--no-fail-closed",
        dest="fail_closed",
        action="store_false",
        help="skip the decider-disconnects-mid-hold fail-closed phase",
    )
    p.add_argument(
        "--no-multi-decider",
        dest="multi_decider",
        action="store_false",
        help="skip the multiple-deciders (first-wins + sync) phase",
    )
    p.add_argument(
        "--snapshot",
        dest="snapshot",
        action="store_true",
        help="enable the reconnect snapshot-replay phase (DEFAULT OFF: with "
        "real multi-IP hosts the result is indeterminate — a "
        "resolved-while-away row is indistinguishable from a CDN "
        "IP-cascade respawn — so it records a finding, not a pass/fail)",
    )
    p.add_argument(
        "--no-revoke",
        dest="revoke",
        action="store_false",
        help="skip the revoke-verdict phase (#2339/#2396)",
    )
    p.add_argument(
        "--no-rejected",
        dest="rejected",
        action="store_false",
        help="skip the rejected_domains pre-emptive-deny phase (#2367)",
    )
    p.add_argument(
        "--no-static-phase",
        dest="static_phase",
        action="store_false",
        help="skip the no-decider static-mode phase (off-list must be denied)",
    )
    p.add_argument(
        "--no-static-refusal",
        dest="static_refusal",
        action="store_false",
        help="skip the #2395 static-workspace-refuses-decider check",
    )
    p.add_argument(
        "--expiry-wait",
        type=float,
        default=9.0,
        help="seconds to wait past a 5s verdict before the exceeding probe (default 9)",
    )
    p.add_argument(
        "--keep-workspace",
        dest="delete_workspace",
        action="store_false",
        help="do not delete the workspace on exit",
    )
    args = p.parse_args()
    if args.seed is None:
        args.seed = random.randrange(1 << 30)
    if args.consent_timeout < 4:
        p.error("--consent-timeout must be >= 4")
    return asyncio.run(SmokeTest(args).run())


if __name__ == "__main__":
    raise SystemExit(main())
