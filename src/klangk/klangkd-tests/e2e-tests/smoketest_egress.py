#!/usr/bin/env python3
"""Interactive-egress fuzz smoketest (#2392).

Standalone, human-run, not in CI (no ``test_`` prefix). Invoke under devenv:

    devenv --quiet shell -- \\
        python src/klangk/klangkd-tests/e2e-tests/smoketest_egress.py [--count N]

Add ``--as-member`` (#2976) to run the consent flow as a plain workspace
member (coders role, no instance-admin grants) instead of the seeded
admin; the admin only bootstraps (member user, workspaces, role shares).

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
import atexit
import json
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

# Import sibling e2e helpers regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import httpx  # noqa: E402
import websockets  # noqa: E402

from _e2e_server import start_server, stop_server, tracked_mkdtemp, ws_connect  # noqa: E402

from _controlled_dns import (  # noqa: E402
    ControlledDns,
    cleanup_stale_containers,
)

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
    make_pause,
    make_ping,
    make_revoke,
    make_unpause,
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

# The --as-member acting identity (#2976): a plain user with no instance
# grants. The seeded admin creates + shares workspaces to this user's
# coders role; every consent operation then runs on this token.
MEMBER_EMAIL = "smoke-member@example.com"
MEMBER_PASSWORD = "smokepass"
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
# Throwaway off-list hosts for the sidecar-readiness probe (#2417). Fresh (not
# in the fuzz pool or any phase) so a probe can't perturb the run's model, and
# enough of them that cycling back to one -- after a fail-close forged RST
# installs a ~10s REJECT backstop on its IP -- lands well past that backstop.
_SIDECAR_PROBE_HOSTS = [
    "ietf.org",
    "iana.org",
    "w3.org",
    "isc.org",
    "openssl.org",
    "python.org",
    "rust-lang.org",
    "gnupg.org",
]
# Mixed-users phase (#3112): the second identity's email. The member is
# always ensured + shared into the workspace's coders role (both identities
# must hold egress-consent); the SECOND decider runs on the member's token by
# default (the seeded admin acts) and on the admin's token under --as-member
# (the member acts).
ADMIN_EMAIL = "smoke@example.com"
# Fresh real destinations for the mixed-users phase (#3112), untouched by
# the fuzz pool, the probe list, or any other phase, so each check starts
# from a clean consent state.
_MIXED_HOSTS = {
    "visibility": "sqlite.org",
    "late_other": "gnu.org",
    "late_acting": "debian.org",
    "carryover": "mozilla.org",
}
# Concurrent fan-out size for the per-connection-cache phase (#2441): N
# distinct hosts fired at once must surface N distinct held requests, proving
# the post-#2331 NFQUEUE consumer holds N flows concurrently (not serialized).
# >2 steps beyond the existing A/B held pair; bounded by the controlled-DNS IP
# slice (each host gets its own stable IP).
_FANOUT_N = 5
# Worst-case sidecar connect backoff (1->2->4->8->15s) plus margin for the
# workspace JWT file to appear; the readiness probe retries fresh hosts across
# this whole window before giving up.
_SIDECAR_READY_BUDGET = 60.0
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


def _trigger(
    container: str,
    host: str,
    outfile: str,
    port: int = 443,
    scheme: str = "https",
) -> None:
    # Detached curl: blocks on the held SYN until a verdict/timeout, then writes
    # EXIT:$?. -k skips cert validation so an IP/cert-mismatch still gives a
    # clean connect(0)/refuse(7) signal. ``port``/``scheme`` let the port-scope
    # phase drive :80 (http) vs :443 (https) on the same controlled IP (#2424).
    subprocess.run(
        [
            "podman",
            "exec",
            "-d",
            container,
            "bash",
            "-c",
            "curl -sS -k --max-time 30 -o /dev/null "
            f"{scheme}://{shlex.quote(host)}:{port} > {outfile} 2>&1; "
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


def _container_inspect(container: str) -> dict | None:
    """``podman inspect`` one container -> parsed JSON, or ``None`` if gone.

    Used by the container-gone diagnostics (#2475) to read the death state
    (status / exit code / OOMKilled) before teardown removes the corpse.
    """
    try:
        r = subprocess.run(
            ["podman", "inspect", "--format", "json", container],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        arr = json.loads(r.stdout or "[]")
        return arr[0] if arr else None
    except (ValueError, IndexError):
        return None


def _is_container_gone_error(exc: BaseException) -> bool:
    """Did a ``podman exec`` fail because the target container vanished?

    ``_trigger`` runs ``podman exec -d ...`` with ``check=True`` and no output
    capture, so a mid-run container removal surfaces as a
    ``CalledProcessError(returncode=125)`` whose stderr ('no such container')
    is NOT on the exception -- it already went to the terminal. Podman's 125 is
    its runtime-error code; a detached ``exec`` only returns 125 when it can't
    reach the container, so rc 125 + a ``podman exec`` command is a reliable
    gone-signal. Used to fork the scored loop's per-step handler into a single
    CONTAINER-GONE finding + abort instead of a spurious cascade (#2475).
    """
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    cmd = list(getattr(exc, "cmd", None) or [])
    if not cmd or cmd[0] != "podman" or "exec" not in cmd:
        return False
    return exc.returncode == 125


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


def _pending_rids_for(app: ConsentDeciderApp, canon: str) -> list[str]:
    """Every pending request id whose canonical dest is ``canon`` (any count).

    Unlike :func:`_pending_for` (which returns one), this keeps duplicates --
    used by the per-connection-cache phase to count how many distinct requests
    surfaced for the SAME host (two concurrent connections must produce two,
    not one; a held connection's retransmits must produce one, not many).
    """
    return [
        req.id
        for req in app.controller.pending.values()
        if _canonical(req.dest_host or "") == canon
    ]


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


async def _wait_all_hosts(
    app, canons: list[str], timeout: float = 15.0
) -> dict[str, str] | None:
    """Wait until every canonical host in ``canons`` has >=1 pending request,
    ALL held simultaneously.

    Returns ``{canon: rid}`` once every host is present at once, or None on
    timeout. For the N-way fan-out (#2337): N concurrent connects to N distinct
    hosts must each surface a request AT THE SAME TIME; a serialized NFQUEUE
    consumer (pre-#2331) holds only the first until it resolves, so the rest
    never appear together.
    """
    want = set(canons)
    deadline = time.time() + timeout
    while time.time() < deadline:
        present: dict[str, str] = {}
        for req in app.controller.pending.values():
            c = _canonical(req.dest_host or "")
            if c in want and c not in present:
                present[c] = req.id
        if want <= present.keys():
            return {c: present[c] for c in canons}
        await asyncio.sleep(0.2)
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
    the fuzz loop); this is a lightweight extra connection for the
    multi-decider / snapshot-replay / decider-scope / audit phases, speaking
    the same ``egress_request`` / ``egress_resolved`` / ``verdict`` frames.

    ``ping=True`` starts a keepalive pinger so the registry's 45s liveness
    sweep (#2308) can't reap the decider mid-phase -- matters for phases that
    wait on a second workspace's container or on a consent timeout. The shared
    multi-decider d2 stays non-pinging by design: its reaping inside the 45s
    window is a backstop for the no-decider phase, and it is closed explicitly
    anyway (#2413).
    """

    def __init__(self, ws, *, ping: bool = False) -> None:
        self.ws = ws
        self.held: dict[str, str] = {}  # request_id -> canonical host
        # request_id -> the egress_resolved frame's ``decision`` field
        # ("allowed"/"denied"/"expired") -- the observable audit distinction
        # between a human verdict and a consent-timeout auto-expire (#2392).
        self.resolved: dict[str, str | None] = {}
        # canon -> latest in-effect verdict row from an ``egress_rules``
        # broadcast (carries ``decided_by``; the mixed-users phase's
        # attribution check, #3112).
        self.rules: dict[str, dict] = {}
        self._ping_task: asyncio.Task | None = None
        if ping:
            self.start_pinger()

    async def _recv(self, timeout: float):
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        except Exception:
            return (None, None)
        try:
            msg = json.loads(raw)
        except Exception:
            return ("ignored", None)
        return self._apply(msg)

    def _apply(self, msg: dict) -> tuple[str, object | None]:
        # Update held/resolved from one parsed frame; return (type, payload).
        # Factored out so the pause-ack waiter can re-enter it for any
        # egress_request/egress_resolved frame that lands alongside the ack.
        t = msg.get("type")
        if t == "egress_rules":
            rows = list(msg.get("allowed") or []) + list(
                msg.get("denied") or []
            )
            self.rules = {
                _canonical(str(r.get("dest_host") or "")): r for r in rows
            }
            return ("rules", msg)
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
                self.resolved[rid] = msg.get("decision")
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

    async def wait_resolution(
        self, rid: str, timeout: float = 20.0
    ) -> str | None:
        """The egress_resolved ``decision`` for rid, or None on timeout.

        Drains frames while polling so the egress_resolved actually lands.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self.resolved:
                return self.resolved[rid]
            await self._recv(0.4)
        return None

    async def wait_rule(
        self, canon: str, timeout: float = 12.0
    ) -> dict | None:
        """The in-effect verdict row for ``canon``, or None on timeout.

        Polls while draining so the refreshed ``egress_rules`` broadcast
        (sent after each verdict/revoke) actually lands (#3112).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if canon in self.rules:
                return self.rules[canon]
            await self._recv(0.4)
        return None

    def has(self, canon: str) -> bool:
        return any(h == canon for h in self.held.values())

    async def wait_no_request(
        self, canon: str, window: float = 6.0
    ) -> str | None:
        # Mirror of the module-level _wait_no_request for a RawDecider: None
        # if no request for ``canon`` surfaces in ``window``, else its id.
        # Drains frames while polling so any egress_request actually lands.
        deadline = time.time() + window
        while time.time() < deadline:
            for rid, h in self.held.items():
                if h == canon:
                    return rid
            await self._recv(0.3)
        return None

    async def pause(self, duration: str, timeout: float = 10.0) -> bool:
        # #2332: send a pause frame; True once a pause_ack with ok=True lands.
        await self.ws.send(make_pause(duration))
        return await self._await_pause_ack(timeout)

    async def unpause(self, timeout: float = 10.0) -> bool:
        # #2332: clear an active pause; True once a pause_ack ok=True lands.
        await self.ws.send(make_unpause())
        return await self._await_pause_ack(timeout)

    async def _await_pause_ack(self, timeout: float) -> bool:
        # The coordinator sets the pause then sends the ack, then broadcasts a
        # refreshed egress_rules frame; apply any egress frame that arrives
        # alongside so held/resolved stay consistent.
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.5)
            except Exception:
                return False
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "pause_ack":
                return bool(msg.get("ok"))
            self._apply(msg)
        return False

    async def verdict(self, rid: str, decision: str, duration: str) -> None:
        await self.ws.send(make_verdict(rid, decision, duration))

    async def revoke(self, rid: str) -> None:
        await self.ws.send(make_revoke(rid))

    def start_pinger(self) -> None:
        """Send client pings so the registry reaper doesn't drop us (#2308)."""
        if self._ping_task is None:
            self._ping_task = asyncio.create_task(self._ping_loop())

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(15.0)
                try:
                    await self.ws.send(make_ping())
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ping_task = None
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


# -- outcome names ----------------------------------------------------------
# A stable NAME per observable phenomenon (independent of severity), printed on
# each result line and tallied in the summary so a repeat in a later run maps
# straight to the issue that explains it. Keyed on the result's `detail` prefix
# (the most specific signal); the `_classify_conn` mismatches carry no detail,
# so `_outcome_name` falls back to (expect_conn, exit_code). PASS rows get no
# name. See #2421.
#
# `OUTCOME_NAMES` is the single source of truth: every name a prefix or
# expect_conn maps to must be a key here (checked at import below), and each
# entry carries the one-line description + issue(s) that explain the class.
_DETAIL_PREFIX_NAMES: list[tuple[str, str]] = [
    ("expected a request, none arrived", "NO-EXPECTED-REQUEST"),
    ("expected a request; connection hung", "HUNG-NFQUEUE-DNS"),
    ("expected no request (covered:allowed:allowlist", "ALLOWLIST-PROMPTED"),
    ("expected no request (covered:", "CARRYOVER-SURPRISE"),
    ("expected a re-prompt; connection hung", "HUNG-NFQUEUE-DNS"),
    ("expected a re-prompt; none arrived", "NO-EXPECTED-REQUEST"),
    ("verdict not in effect", "CARRYOVER-SURPRISE"),
    ("no request for a fresh off-list host", "NO-EXPECTED-REQUEST"),
    ("could not attach a 2nd decider", "DECIDER2-HANDSHAKE"),
    ("X not seen by BOTH deciders", "NO-EXPECTED-REQUEST"),
    ("Y not held", "NO-EXPECTED-REQUEST"),
    ("app didn't see decider2's resolve", "CONN-NOT-CLEAN"),
    ("decider2's deny let the connection through", "DENY-RELEASED"),
    ("connection not cleanly refused", "CONN-NOT-CLEAN"),
    ("B conn not cleanly refused", "CONN-NOT-CLEAN"),
    ("B2 conn not cleanly refused", "CONN-NOT-CLEAN"),
    ("late allow let it through", "FIRST-DECISION-VIOLATION"),
    ("A/B hung", "AB-HELD-HUNG"),
    ("A/B not both held", "NO-EXPECTED-REQUEST"),
    ("A present", "SNAPSHOT-CASCADE"),
    ("B missing from snapshot", "SNAPSHOT-CASCADE"),
    ("a decider registered against a static workspace", "STATICWS-ACCEPTED"),
    ("neither a frame nor a close", "STATICWS-ACCEPTED"),
    ("a rejected host prompted for consent", "REJECTED-PROMPTED"),
    ("rejected host hung", "HUNG-NFQUEUE-DNS"),
    ("rejected host succeeded", "REJECTED-LEAK"),
    ("off-list host did not prompt", "NO-EXPECTED-REQUEST"),
    ("off-list host was not held for consent", "NO-EXPECTED-REQUEST"),
    ("post-revoke connection hung", "HUNG-NFQUEUE-DNS"),
    ("no re-prompt after revoke", "NO-REPROMPT-REVOKE"),
    ("connection hung after disconnect", "HUNG-NFQUEUE-DNS"),
    ("connection SUCCEEDED after the decider disconnected", "FAILCLOSED-LEAK"),
    ("B sidecar not ready", "NO-EXPECTED-REQUEST"),
    ("A-scoped decider saw B's request", "ISOLATION-BROKEN"),
    ("B deny let the connection through", "DENY-RELEASED"),
    ("deploy-wide connect was accepted", "DEPLOYWIDE-ACCEPTED"),
    ("no-workspace handshake failed unexpectedly", "UNEXPECTED-ERROR"),
    ("could not set up the scope phase", "UNEXPECTED-ERROR"),
    ("no hold surfaced", "NO-EXPECTED-REQUEST"),
    ("expired but the connection succeeded", "NORESPONSE-OK"),
    ("timeout audited as", "AUDIT-MISLABELED"),
    ("no egress_resolved frame", "CONN-NOT-CLEAN"),
    ("unexpected resolution", "AUDIT-MISLABELED"),
    ("denied but conn not refused", "CONN-NOT-CLEAN"),
    ("human deny audited as", "AUDIT-MISLABELED"),
    ("audit phase failed", "UNEXPECTED-ERROR"),
    ("allow-list host should connect", "ALLOW-REFUSED"),
    ("off-list hung (not a clean static denial)", "STATIC-HELD"),
    ("off-list succeeded with no decider", "STATIC-LEAK"),
    ("step raised", "UNEXPECTED-ERROR"),
    ("container gone", "CONTAINER-GONE"),
    # Controlled-DNS phases (#2424): host-scope / port-scope / snapshot-replay.
    ("A replayed in reconnect snapshot", "SNAPSHOT-REPLAY"),
    ("B missing from reconnect snapshot", "SNAPSHOT-REPLAY"),
    ("host-scope phase failed", "UNEXPECTED-ERROR"),
    ("host scope: ", "HOST-SCOPE-VIOLATION"),
    ("port-scope phase failed", "UNEXPECTED-ERROR"),
    ("port scope: ", "PORT-SCOPE-VIOLATION"),
    # Co-resident hosts (#2440): pin per-IP allow/revoke sharing (canary for #2352).
    ("co-resident: allow A did NOT leak to B", "CORESIDENT-ALLOW-FLIP"),
    ("co-resident: revoke A uncovered B", "CORESIDENT-REVOKE-FLIP"),
    ("co-resident: A did NOT prompt", "NO-EXPECTED-REQUEST"),
    ("co-resident: post-revoke B hung", "HUNG-NFQUEUE-DNS"),
    ("co-resident: post-revoke B not clean", "HUNG-NFQUEUE-DNS"),
    ("co-resident: B did not connect cleanly", "HUNG-NFQUEUE-DNS"),
    ("co-resident phase failed", "UNEXPECTED-ERROR"),
    # allow egress mode (#2411): off-list must connect (coordinator record+allow),
    # rejected still denied, decider registration refused.
    ("allow-mode off-list refused", "ALLOWMODE-REFUSED"),
    ("allow-mode off-list hung", "ALLOWMODE-REFUSED"),
    ("allow-mode sidecar consent-WS did not wire", "HUNG-NFQUEUE-DNS"),
    (
        "a decider registered against an allow workspace",
        "ALLOWMODE-DECIDER-ACCEPTED",
    ),
    ("allow decider: no frame/close", "ALLOWMODE-DECIDER-ACCEPTED"),
    ("allow-mode phase failed", "UNEXPECTED-ERROR"),
    # restart verdict semantics (#2364/#2346): forever survives, tilrestart reaped.
    ("restart-phase sidecar not wired", "HUNG-NFQUEUE-DNS"),
    ("restart-phase no request for", "NO-EXPECTED-REQUEST"),
    ("tilrestart not in effect pre-restart", "CARRYOVER-SURPRISE"),
    ("forever-allow did not survive restart", "RESTART-FOREVER-NOT-DURABLE"),
    ("forever-deny did not survive restart", "RESTART-FOREVER-NOT-DURABLE"),
    ("forever-deny leaked after restart", "RESTART-FOREVER-NOT-DURABLE"),
    ("tilrestart survived restart", "RESTART-TILRESTART-SURVIVED"),
    ("restart-phase failed", "UNEXPECTED-ERROR"),
    # per-connection verdict cache + N-way fan-out (#2441 / #2361 / #2366 / #2337).
    ("fan-out: only", "FANOUT-SERIALIZED"),
    ("fan-out: not", "FANOUT-SERIALIZED"),
    (
        "concurrent same-host connections produced",
        "CONCURRENT-NOT-DEDUPED",
    ),
    (
        "a once verdict on one connection released a separate",
        "ONCE-CROSS-CONN",
    ),
    ("a held connection produced", "RETRANSMIT-DUPLICATED"),
    ("no hold surfaced for the", "NO-EXPECTED-REQUEST"),
    ("concurrent same-host deduped to one prompt, but both", "ALLOW-REFUSED"),
    ("fan-out phase failed", "UNEXPECTED-ERROR"),
    # mixed users (#3112): two identities decide one workspace at once.
    ("not seen by BOTH users", "MIXEDUSER-NOT-SHARED"),
    ("the app didn't see the other user's resolve", "CONN-NOT-CLEAN"),
    ("first cross-user deny", "CONN-NOT-CLEAN"),
    (
        "late cross-user verdict took effect",
        "MIXEDUSER-LATE-VERDICT-APPLIED",
    ),
    ("no re-prompt after the late verdict", "HUNG-NFQUEUE-DNS"),
    ("decided_by=", "AUDIT-MISATTRIBUTED"),
    ("no egress_rules frame", "CONN-NOT-CLEAN"),
    ("cross-user 1h allow did not cover", "MIXEDUSER-CARRYOVER-BROKEN"),
    ("revoke by the non-deciding user", "MIXEDUSER-REVOKE-BROKEN"),
    ("user id lookup missed", "MIXEDUSER-SETUP"),
    ("other user's allow was refused", "ALLOW-REFUSED"),
    ("other user's allow:", "HUNG-NFQUEUE-DNS"),
    ("cross-user covered allow was refused", "ALLOW-REFUSED"),
    ("cross-user covered allow:", "HUNG-NFQUEUE-DNS"),
    ("mixed-users phase failed", "UNEXPECTED-ERROR"),
    ("run aborted", "UNEXPECTED-ERROR"),
]

OUTCOME_NAMES: dict[str, str] = {
    "NO-EXPECTED-REQUEST": "an expected consent request never arrived (off-list / post-expiry / post-revoke); fail-closed or hung. #2417 #2418",
    "CARRYOVER-SURPRISE": "an in-effect verdict didn't cover a retry (CDN/per-IP). Non-bug per #2399; scored as a FINDING (#2419).",
    "DECIDER2-HANDSHAKE": "a 2nd consent decider WS opening-handshake failure. #2420",
    "ALLOW-REFUSED": "an allow / allow-list / active-allow was refused (EXPECT_RELEASED -> exit 7).",
    "DENY-RELEASED": "a deny / active-deny let the connection through (EXPECT_REFUSED -> exit 0).",
    "NORESPONSE-OK": "a no-response (timeout) succeeded (EXPECT_NOT0 -> exit 0).",
    "STATIC-LEAK": "off-list succeeded with no decider registered.",
    "STATIC-HELD": "off-list hung with no decider (not a clean static denial).",
    "STATICWS-ACCEPTED": "a decider registered against a static workspace. #2395",
    "REJECTED-PROMPTED": "a rejected_domains host prompted for consent (not pre-empted). #2367",
    "REJECTED-LEAK": "a rejected_domains host succeeded (leak). #2367",
    "NO-REPROMPT-REVOKE": "no re-prompt after a revoke (rule not dropped). #2339 #2396",
    "FAILCLOSED-LEAK": "connection succeeded after the decider disconnected (#2308 violation).",
    "FIRST-DECISION-VIOLATION": "first-decision-wins broken (a late/2nd verdict let it through).",
    "SNAPSHOT-CASCADE": "reconnect snapshot indeterminate (CDN-cascade respawn / timing).",
    "HUNG-NFQUEUE-DNS": "connection hung / unexpected exit (NFQUEUE/DNS), not a consent failure.",
    "CONTAINER-GONE": "the workspace container vanished mid-run (podman exec rc 125 'no such container') -- an infra/networking failure, not a consent-logic break. Death diagnostics captured. #2475",
    "CONN-NOT-CLEAN": "connection not cleanly refused / a resolve frame wasn't seen.",
    "AB-HELD-HUNG": "concurrent A/B hold hung (NFQUEUE/DNS; possibly a concurrent-hold issue).",
    "ISOLATION-BROKEN": "a workspace-scoped decider saw another workspace's request. #2392",
    "DEPLOYWIDE-ACCEPTED": "a no-workspace decider handshake was accepted -- the cross-workspace consent path is back. #2976",
    "UNEXPECTED-ERROR": "a phase bring-up or per-step exception (an unexpected error).",
    "AUDIT-MISLABELED": "expired/denied audit distinction broken (timeout audited as deny, or vice-versa). #2392",
    "ALLOWLIST-PROMPTED": "an allow-list-covered host prompted for consent (a real static-allow invariant break). #2419",
    "HOST-SCOPE-VIOLATION": "an nginx-style host scope (exact/inclusive/subdomains, #2377) let the wrong name through / blocked the right one.",
    "PORT-SCOPE-VIOLATION": "a port-scoped allow (host:443) permitted a different port (:80), or vice-versa.",
    "CORESIDENT-ALLOW-FLIP": "co-resident allow canary flipped: allowing host A no longer leaked to host B on the same IP -- per-host allow may have landed (#2352/#2440).",
    "CORESIDENT-REVOKE-FLIP": "co-resident revoke canary flipped: revoking host A now uncovers host B on the same IP (B re-prompted post-revoke) -- per-host or reverse-index revoke may have landed (#2352/#2440); update the pin.",
    "SNAPSHOT-REPLAY": "a resolved-while-away row replayed (or a held row vanished) in the reconnect snapshot -- now deterministic under controlled DNS (#2424).",
    "ALLOWMODE-REFUSED": "an allow-mode (default-permit) off-list host was refused or hung -- it must connect (#2411/#2406).",
    "ALLOWMODE-DECIDER-ACCEPTED": "a consent decider registered (or ambiguously attached) against an allow-mode workspace -- it must be refused, same gate as static (#2395/#2406).",
    "RESTART-FOREVER-NOT-DURABLE": "a `forever` verdict did not survive a container restart -- forever-allow must reconnect via allowed_domains (#2368), forever-deny must re-deny via rejected_domains (#2369).",
    "RESTART-TILRESTART-SURVIVED": "a `tilrestart` verdict survived a container restart -- it must be reaped (the sidecar's in-memory rule dies and the DB row is cleared on restart, #2346/#2349).",
    "PAUSE-HELD": "an off-list host surfaced a consent request while consent filtering was paused -- the pause did not silence the interactive hold. #2332",
    "PAUSE-REFUSED": "an off-list host was refused or hung while paused instead of auto-allowing -- the pause did not take effect at the gate. #2332",
    "PAUSE-POLICY-BYPASS": "a static policy (allow-list / rejected_domains) was bypassed or changed while paused -- pause must affect only the interactive hold, not the DNS-layer lists. #2332",
    "PAUSE-RESUME-BROKEN": "after resume, an off-list host was not re-held for consent (the pause lingered, or the re-hold failed). #2332",
    "PAUSE-ACK-FAILED": "the pause/unpause pause_ack came back not-ok (protocol: unknown duration). #2332/#2389/#2883",
    "FANOUT-SERIALIZED": "N concurrent off-list connects did not all surface a held request simultaneously (the NFQUEUE consumer serialized them -- a #2331/#2337 regression).",
    "CONCURRENT-NOT-DEDUPED": "concurrent connections to the same destination each produced a prompt (the coordinator's per-destination dedup is broken -- each should collapse to one).",
    "ONCE-CROSS-CONN": "a `once` verdict on one connection released a separate concurrent connection to the same host (the verdict leaked across connections -- #2361).",
    "RETRANSMIT-DUPLICATED": "a held connection's SYN retransmits produced more than one consent request (the in-flight dedup is broken -- #2366).",
    "MIXEDUSER-NOT-SHARED": "a pending request was not visible to both users' deciders (workspace-scoped fanout broken across users). #3112",
    "MIXEDUSER-LATE-VERDICT-APPLIED": "a late verdict from the other user took effect (first-decision-wins broken across users). #3112",
    "MIXEDUSER-CARRYOVER-BROKEN": "a carrying verdict issued by one user did not cover a later connection while the other user's decider watched. #3112",
    "MIXEDUSER-REVOKE-BROKEN": "a revoke by the non-deciding user did not drop the other user's verdict (no re-prompt). #3112",
    "AUDIT-MISATTRIBUTED": "an egress_rules verdict row's decided_by names the wrong user (NULL or the other identity). #3112",
    "MIXEDUSER-SETUP": "mixed-users phase bring-up failed (member login, user id lookup). #3112",
}

_EXPECT_CONN_NAMES = {
    EXPECT_RELEASED: "ALLOW-REFUSED",
    EXPECT_REFUSED: "DENY-RELEASED",
    EXPECT_NOT0: "NORESPONSE-OK",
}

# Every name the mapping tables emit must be registered in OUTCOME_NAMES; a
# typo here fails loudly at import instead of producing a mystery "??".
assert all(name in OUTCOME_NAMES for _, name in _DETAIL_PREFIX_NAMES), (
    "unregistered outcome name in _DETAIL_PREFIX_NAMES"
)
assert all(name in OUTCOME_NAMES for name in _EXPECT_CONN_NAMES.values()), (
    "unregistered outcome name in _EXPECT_CONN_NAMES"
)


def _outcome_name(res: "_Result") -> str:
    """Stable outcome name for a result ("" for PASS). See #2421.

    A non-PASS result whose detail matches no known prefix returns the sentinel
    "??" so any gap is loud rather than silent.
    """
    if res.status == PASS:
        return ""
    detail = res.detail or ""
    for prefix, name in _DETAIL_PREFIX_NAMES:
        if detail.startswith(prefix):
            return name
    if detail:
        return "??"  # unmapped detail -> visible gap
    # No detail: a `_classify_conn` outcome. Derive from the expectation.
    if res.status == MISMATCH:
        return _EXPECT_CONN_NAMES.get(res.expect_conn, "??")
    # FINDING with no detail -> hung / unexpected exit (NFQUEUE/DNS).
    return "HUNG-NFQUEUE-DNS"


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
    outcome: str = ""  # stable outcome name; "" for PASS. See #2421.


@dataclass
class _Summary:
    total: int = 0
    passed: int = 0
    findings: int = 0
    mismatches: int = 0
    rows: list[_Result] = field(default_factory=list)


def _mixed_release_detail(label: str, status: str, ec: int | None) -> str:
    """Detail for an expect-released classify row so its outcome name maps.

    A bare ``_classify_conn`` status recorded with no detail would fall
    through ``_outcome_name`` to the ``??`` gap (probe rows carry no
    ``expect_conn``); give MISMATCH/FINDING rows a mapped prefix instead.
    """
    if status == MISMATCH:
        return f"{label} was refused"
    if status == FINDING:
        return f"{label}: unexpected exit ({_exit_label(ec)})"
    return ""


class SmokeTest:
    def __init__(self, args) -> None:
        self.args = args
        self.model = EgressModel(_ALLOW_LIST)
        self.summary = _Summary()
        self._owned_server: dict | None = None
        self.server: dict | None = None
        self.auth: dict | None = None
        # The bootstrap identity (seeded admin): workspace create/delete,
        # member provisioning, and the no-workspace decider probe. Equals
        # self.auth unless --as-member splits them.
        self.admin_auth: dict | None = None
        self.ws_id: str | None = None
        self.ws_conn = None
        self._drain_task: asyncio.Task | None = None
        self.app: ConsentDeciderApp | None = None
        self.container: str | None = None
        self._shared_d2: RawDecider | None = None
        # The controlled-DNS fixture (#2424): None unless --controlled-dns is on.
        # When started it points the real sidecar's upstream at a test DNS that
        # resolves chosen names to single stable IPs (fronted by a test HTTP/HTTPS
        # server), removing the CDN-IP-rotation + L3/L4 confounds that made the
        # snapshot-replay / host-scope / port-scope phases indeterminate.
        self.dns: ControlledDns | None = None
        # Path to a temp containers.conf (netns=bridge) the smoketest writes when
        # controlled DNS is on, so klangkd's sidecar + the fixture's containers
        # land on the same bridge network (the default rootless netns here is
        # pasta, which isolates containers -- the sidecar couldn't reach the
        # fixture's DNS/target containers). Cleared in teardown.
        self._containers_conf: str | None = None
        # Extra deciders / workspaces / terminal-WS opened by the decider-scope
        # + audit phases. Closed + deleted in teardown so nothing leaks into
        # the no-decider phase (the #2413 leak class) or past the run. A phase
        # also closes its own deciders eagerly: a live one keeps its workspace
        # interactive and would break the no-decider phase premise.
        self._extra_deciders: list[RawDecider] = []
        self._extra_ws_ids: list[str] = []
        self._extra_ws_conns: list[tuple] = []
        # Guard for _cleanup_sync(): the container-removing teardown (dns.stop
        # + stop_server) must run exactly once even though it is reachable
        # from the async finally, atexit, and the SIGTERM handler (#2443).
        self._cleaned_up = False

    # -- interrupt-safe cleanup -----------------------------------------
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True iff a process with ``pid`` exists (mirrors klangkd's check)."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # another user's process -> assume alive
        return True

    def _cleanup_sync(self) -> None:
        """Synchronous, idempotent container teardown (#2443).

        Removes the ctrl-dns fixtures (``self.dns.stop()``) and the owned
        klangkd subprocess + its labelled sidecar containers
        (``stop_server()``). Both are synchronous and need no event loop, so
        this is safe to call from ``atexit`` and a ``SIGTERM`` handler — the
        paths the async ``teardown()`` never reaches when the smoketest is
        killed (Ctrl-C under asyncio runs the async ``finally``, but
        ``SIGTERM``/``SIGKILL`` terminate with no cleanup, leaking the
        unlabelled ctrl-dns fixtures forever and orphaning the klangkd).

        Idempotent and best-effort: the async ``teardown()`` nulls these
        fields after its own cleanup, so a double-call is a no-op.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True
        dns = self.dns
        owned = self._owned_server
        # Null first so the async teardown (which may run concurrently on a
        # Ctrl-C) sees nothing to do and cannot race us.
        self.dns = None
        self._owned_server = None
        if dns is not None:
            try:
                dns.stop()
            except Exception:
                pass
        if owned is not None:
            try:
                stop_server(owned)
            except Exception:
                pass

    def _capture_sidecar_logs(self) -> None:
        """Dump each workspace's network-sidecar podman log to /tmp (#2464).

        The forged eager-deny RST is the sidecar's primary fast-refuse; when a
        denied connection instead *times out* (exit 28), the only way to see
        whether the RST fired is the sidecar's own stdout -- which podman
        discards when the sidecar is removed. This runs in ``run()``'s
        ``finally`` BEFORE ``teardown()`` deletes the workspaces (and their
        sidecars), so the logs survive. Targets only this run's workspaces
        (by ``klangk.workspace`` label) so an operator's unrelated sidecars
        aren't touched. Best-effort: no podman / no sidecar -> silent no-op.
        """
        ws_ids = (
            [self.ws_id, *self._extra_ws_ids]
            if self.ws_id
            else list(self._extra_ws_ids)
        )
        for ws_id in ws_ids:
            try:
                ps = subprocess.run(
                    [
                        "podman",
                        "ps",
                        "-a",
                        "--filter",
                        f"label=klangk.workspace={ws_id}",
                        "--filter",
                        "label=klangk.role=network-sidecar",
                        "--format",
                        "{{.Names}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except Exception:
                continue
            for name in ps.stdout.split():
                try:
                    logs = subprocess.run(
                        ["podman", "logs", name],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except Exception:
                    continue
                path = f"/tmp/smoke_sidecar_{ws_id[:8]}.log"
                try:
                    with open(path, "w") as f:
                        f.write((logs.stdout or "") + (logs.stderr or ""))
                    print(f"sidecar logs {name} (ws {ws_id[:8]}) -> {path}")
                except Exception:
                    pass

    def _capture_container_diagnostics(
        self, container: str, at_step: int
    ) -> str:
        """Capture why the workspace container vanished, *now* (before teardown
        removes the rest). Returns the diagnostics file path, or '' on failure.

        'no such container' means the container was **removed** (not merely
        stopped) -- and only klangkd removes workspace containers -- so the
        klangkd log tail is the decisive evidence. Also dumps the workspace
        container's inspect state (exit code / OOMKilled, if the corpse is
        still in the store), ``podman ps -a`` for the workspace, and this
        workspace's network-sidecar logs (the eager-deny RST trace, #2464).
        See #2475.
        """
        ws_id = self.ws_id or "?"
        stamp = int(time.time())
        path = f"/tmp/smoke_container_gone_{ws_id[:8]}_{stamp}.log"
        lines: list[str] = [
            f"# container-gone diagnostics @ step {at_step}",
            f"workspace={ws_id} container={container}",
        ]

        info = _container_inspect(container)
        if info is None:
            lines.append(f"podman inspect {container}: <gone/removed>")
        else:
            st = info.get("State", {}) or {}
            lines.append(
                f"podman inspect {container}: status={st.get('Status')} "
                f"exit={st.get('ExitCode')} oom={st.get('OOMKilled')} "
                f"started={st.get('StartedAt')} "
                f"finished={st.get('FinishedAt')}"
            )

        try:
            ps = subprocess.run(
                [
                    "podman",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=klangk.workspace={ws_id}",
                    "--format",
                    "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            lines.append(f"--- podman ps -a (klangk.workspace={ws_id}) ---")
            lines.append((ps.stdout or "(none)").rstrip())
        except Exception as e:  # noqa: BLE001
            lines.append(f"podman ps -a failed: {e!r}")

        # This workspace's network-sidecar logs (eager-deny RST trace; #2464).
        try:
            sc = subprocess.run(
                [
                    "podman",
                    "ps",
                    "-a",
                    "--filter",
                    f"label=klangk.workspace={ws_id}",
                    "--filter",
                    "label=klangk.role=network-sidecar",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            for name in sc.stdout.split():
                try:
                    lg = subprocess.run(
                        ["podman", "logs", name],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    lines.append(f"--- sidecar logs {name} ---")
                    lines.append(
                        ((lg.stdout or "") + (lg.stderr or "")).rstrip()
                    )
                except Exception as e:  # noqa: BLE001
                    lines.append(f"sidecar logs {name} failed: {e!r}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"sidecar list failed: {e!r}")

        # The decisive evidence: who removed the container. klangkd logs every
        # workspace-container stop/remove with its reason.
        log_path = os.environ.get("SMOKE_KLANGKD_LOG")
        if log_path:
            try:
                with open(log_path) as f:
                    tail = f.readlines()[-400:]
                lines.append(
                    f"--- klangkd log tail ({log_path}, "
                    f"last {len(tail)} lines) ---"
                )
                lines.extend(line.rstrip() for line in tail)
            except Exception as e:  # noqa: BLE001
                lines.append(f"klangkd log read failed: {e!r}")
        else:
            lines.append(
                "SMOKE_KLANGKD_LOG unset -- set it to capture the server tail"
            )

        try:
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            return ""
        return path

    # -- setup ------------------------------------------------------------
    def _start_server(self) -> dict:
        kwargs = dict(
            uds=False,
            log_path=os.environ.get("SMOKE_KLANGKD_LOG") or None,
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
            # Opt in the sidecar's per-RST debug logging (#2464): the forged
            # eager-deny RST is the primary fast-refuse, so when a denied
            # connection instead times out (exit 28), the captured sidecar log
            # shows whether each RST fired. Forwarded by
            # ContainerManager.start_network_sidecar; captured to
            # /tmp/smoke_sidecar_*.log by _capture_sidecar_logs at teardown.
            KLANGKNETWORK_EGRESS_DEBUG_RST="1",
            LOGFIRE_TOKEN="",
        )
        # Point the real sidecar's upstream at the controlled-DNS fixture
        # (#2424): when --controlled-dns is on, every workspace the smoketest
        # creates resolves chosen names to single stable test IPs and forwards
        # the rest to a real resolver. Honored verbatim by
        # start_network_sidecar (mirrors the MIN_TTL/SWEEP forwarding above).
        if self.dns is not None:
            kwargs["KLANGKNETWORK_EGRESS_UPSTREAM"] = self.dns.upstream_ip
        try:
            return start_server(**kwargs)
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
    def _login(
        url: str,
        identifier: str = "smoke@example.com",
        password: str = "smokepass",
    ) -> dict:
        client = httpx.Client(base_url=url, timeout=30)
        try:
            r = client.post(
                "/api/v1/auth/login",
                json={"identifier": identifier, "password": password},
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
    def _ensure_member(server: dict, admin: dict) -> None:
        """Create the plain member user for --as-member (idempotent).

        The member gets no admin membership and no manage-* grants --
        consent authority must come from the workspace ACL alone
        (#2976). Since #2569 registration auto-joins the ``members``
        group (instance-role, no permissions beyond the #3137
        create-workspace seed). A 400 "already
        registered" (a rerun against the same server) is tolerated.
        """
        client = httpx.Client(
            base_url=server["url"], headers=admin["headers"], timeout=30
        )
        try:
            r = client.post(
                "/api/v1/users",
                json={"email": MEMBER_EMAIL, "password": MEMBER_PASSWORD},
            )
            if r.status_code not in (
                200,
                201,
            ) and "already registered" not in (r.text or ""):
                raise RuntimeError(
                    f"member create failed: {r.status_code} {r.text}"
                )
        finally:
            client.close()

    @staticmethod
    def _share_coder(server: dict, admin: dict, ws_id: str) -> None:
        """Add the member to the workspace's coders role group.

        Coders hold exactly the member consent profile (#2883): terminal,
        egress-consent, restart, files -- and NOT the owner `*`. Every
        workspace the member acts on must be shared this way (workspace
        creation is admin-only, #2569).
        """
        client = httpx.Client(
            base_url=server["url"], headers=admin["headers"], timeout=30
        )
        try:
            r = client.post(
                f"/api/v1/workspaces/{ws_id}/roles/coders",
                json={"email": MEMBER_EMAIL},
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"member share failed: {r.status_code} {r.text}"
                )
        finally:
            client.close()

    @staticmethod
    def _user_id_by_email(server: dict, admin: dict, email: str) -> str | None:
        """The stable users.id for ``email`` via the admin user list (#3112).

        egress_consent.decided_by records this id, so the mixed-users phase
        compares it against the rules-frame row's attributer.
        """
        client = httpx.Client(
            base_url=server["url"], headers=admin["headers"], timeout=30
        )
        try:
            r = client.get(
                "/api/v1/users", params={"q": email, "page_size": 50}
            )
            if r.status_code != 200:
                raise RuntimeError(
                    f"user lookup failed: {r.status_code} {r.text}"
                )
            for row in r.json().get("users", []):
                if row.get("email") == email:
                    return row["id"]
            return None
        finally:
            client.close()

    async def _mixed_setup(self) -> tuple[dict, str | None]:
        """Provision + log in the second identity for the mixed phase (#3112).

        Returns (other_auth, other_user_id). The member is always ensured
        and shared into the main workspace's coders role (idempotent) so
        BOTH identities hold egress-consent on it; the second identity is
        the member by default (the admin acts) and the bootstrap admin
        under --as-member (the member acts). The id feeds the decided_by
        check; None means the lookup missed and the attribution check
        degrades to a finding.
        """
        await asyncio.to_thread(
            self._ensure_member, self.server, self.admin_auth
        )
        await asyncio.to_thread(
            self._share_coder, self.server, self.admin_auth, self.ws_id
        )
        if self.args.as_member:
            other_auth, other_email = self.admin_auth, ADMIN_EMAIL
        else:
            other_auth = await asyncio.to_thread(
                self._login,
                self.server["url"],
                MEMBER_EMAIL,
                MEMBER_PASSWORD,
            )
            other_email = MEMBER_EMAIL
        other_id = await asyncio.to_thread(
            self._user_id_by_email, self.server, self.admin_auth, other_email
        )
        return other_auth, other_id

    async def _new_workspace(self, *args, **kwargs) -> str:
        """Create a workspace the acting identity can use.

        Creation always runs as the bootstrap (admin) identity
        (deterministic ownership for the share-based --as-member
        wiring) -- and under --as-member the workspace is then shared
        to the member's coders role so the member's decider/terminal
        connections on it are authorized.
        """
        ws_id = await asyncio.to_thread(
            self._create_workspace,
            self.server,
            self.admin_auth,
            *args,
            **kwargs,
        )
        if self.args.as_member:
            await asyncio.to_thread(
                self._share_coder, self.server, self.admin_auth, ws_id
            )
        return ws_id

    @staticmethod
    def _create_workspace(
        server: dict,
        auth: dict,
        allow_list: list[str] | None = None,
        rejected_list: list[str] | None = None,
        name: str | None = None,
        egress_mode: str = "interactive",
    ) -> str:
        client = httpx.Client(
            base_url=server["url"], headers=auth["headers"], timeout=120
        )
        try:
            r = client.post(
                "/api/v1/workspaces",
                json={
                    "name": name
                    or f"smoke-{int(time.time() * 1000) % 100000}",
                    "allowed_domains": (
                        _ALLOW_LIST if allow_list is None else allow_list
                    ),
                    "rejected_domains": (
                        _REJECTED_LIST
                        if rejected_list is None
                        else (rejected_list or [])
                    ),
                    "egress_mode": egress_mode,
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

    async def _open_terminal_ws(self, ws_id: str):
        # Open the workspace terminal WS for ws_id + wait for container_ready.
        # Confirms the container is up; the caller keeps the WS open (drained)
        # so the workspace doesn't idle-stop during the run.
        ws = await ws_connect(self.server, f"/ws?token={self.auth['token']}")
        await ws.send(
            json.dumps({"cmd": "workspace_connect", "workspaceId": ws_id})
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            if msg.get("type") == "container_ready":
                return ws
        await ws.close()
        raise RuntimeError("container_ready not received within 120s")

    async def _wait_container_ready(self) -> None:
        self.ws_conn = await self._open_terminal_ws(self.ws_id)
        self._drain_task = asyncio.create_task(self._drain(self.ws_conn))

    @staticmethod
    async def _drain(ws) -> None:
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    def _enable_bridge_networking(self) -> None:
        """Point podman at a temp containers.conf with ``netns = "bridge"``.

        The fixture's DNS + target containers and klangkd's sidecar must share
        a bridge network so the sidecar can reach them by IP. The default
        rootless netns on many hosts is ``pasta``, which gives each container
        its OWN isolated netns (no inter-container routing) -- under it the
        sidecar cannot reach the fixture, and no controlled-DNS prompt ever
        surfaces (#2424). Setting ``netns = "bridge"`` via CONTAINERS_CONF
        (inherited by both the fixture's podman calls and the owned klangkd
        subprocess) puts them on the shared ``podman`` bridge instead. No-op if
        the ambient CONTAINERS_CONF already sets a netns.
        """

        if os.environ.get("CONTAINERS_CONF"):
            return  # operator supplied their own; respect it
        d = tracked_mkdtemp("klangk-smoke-cc-")
        path = os.path.join(d, "containers.conf")
        with open(path, "w") as f:
            f.write('[containers]\nnetns = "bridge"\n')
        os.environ["CONTAINERS_CONF"] = path
        self._containers_conf = path

    async def setup(self) -> None:
        # Register the interrupt-safe teardown up front so a partial setup
        # failure (or a kill mid-setup) still removes whatever fixtures this
        # far exist. _cleanup_sync is idempotent and no-ops on absent fixtures.
        atexit.register(self._cleanup_sync)
        # Start the controlled-DNS fixture BEFORE the owned klangkd so its
        # upstream IP is known when the sidecar is created (the env is read at
        # workspace/sidecar-creation time). Skipped for an external --server
        # (the operator must set KLANGKNETWORK_EGRESS_UPSTREAM on it themselves)
        # or when --no-controlled-dns is passed.
        if self.args.controlled_dns and not self.args.server:
            self._enable_bridge_networking()
            # Clean slate: reclaim ctrl-dns-* containers left by a prior run
            # that was SIGKILLed (or whose teardown never ran). These carry no
            # klangk.* labels, so klangkd's reaper cannot see them (#2443).
            stale = await asyncio.to_thread(cleanup_stale_containers)
            if stale:
                print(
                    f"reclaimed {len(stale)} stale ctrl-dns container(s): "
                    f"{', '.join(stale)}"
                )
            try:
                self.dns = ControlledDns(image=self.args.sidecar_image)
                self.dns.start()
                print(
                    f"controlled-dns up: sidecar upstream -> "
                    f"{self.dns.upstream_ip} (target slice "
                    f"{self.dns.target_ips[0]}..{self.dns.target_ips[-1]})"
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"!! controlled-dns failed to start: {exc!r}",
                    file=sys.stderr,
                )
                # Remove the partially-started fixture (e.g. a target container
                # created before the readiness probe failed) so it isn't left
                # running. _cleanup_sync/atexit would also catch it, but this is
                # the direct path.
                if self.dns is not None:
                    await asyncio.to_thread(self.dns.stop)
                    self.dns = None
                # Fail hard: controlled-DNS was requested (the default) and the
                # whole point of the fixture is to make the fuzz run
                # deterministic (#2424). Silently degrading to real DNS makes
                # every later phase's result indeterminate while the run still
                # prints a healthy-looking plan line, so the operator must opt
                # out explicitly with --no-controlled-dns to accept real DNS
                # (#2473).
                raise RuntimeError(
                    "controlled-DNS is required for a deterministic fuzz run, "
                    "but the fixture failed to start (see above). Re-run with "
                    "--no-controlled-dns to allow real DNS (the "
                    "snapshot/host-scope/port-scope/co-resident phases will be "
                    "skipped)."
                ) from exc
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
        self.admin_auth = await asyncio.to_thread(
            self._login, self.server["url"]
        )
        self.auth = self.admin_auth
        if self.args.as_member:
            # #2976 posture: run the whole consent flow as a plain member.
            # The admin only bootstraps (member user + workspace + coders
            # share); the member's authority is the workspace ACL alone.
            await asyncio.to_thread(
                self._ensure_member, self.server, self.admin_auth
            )
            self.auth = await asyncio.to_thread(
                self._login,
                self.server["url"],
                MEMBER_EMAIL,
                MEMBER_PASSWORD,
            )
            print(
                f"acting as member {MEMBER_EMAIL} "
                f"(coders role; no instance-admin grants)"
            )
        self.ws_id = await self._new_workspace()
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

    async def _wait_sidecar_ready(self, pilot) -> None:
        """Gate the scored loop on the sidecar consent WS being wired (#2417).

        ``_wait_connected`` only proves the *decider's* WS reached klangkd. The
        sidecar's own consent-WS client -- a separate connection from inside the
        container -- connects on its own backoff schedule, gated on the
        workspace JWT file. If the scored loop starts before the sidecar WS is
        up, an off-list SYN hits the NFQUEUE fail-close branch (forge RST + a
        short REJECT backstop, #2415): no egress frame reaches the decider, so
        the iteration scores a hard MISMATCH. Probe a throwaway off-list host
        and retry until a request actually surfaces in the decider (proving
        sidecar-WS -> coordinator -> decider are wired), then settle it with an
        allow/once (releases just this SYN; no learned rule) so the probe can't
        perturb the scored run.
        """
        print(
            "verifying sidecar consent WS is wired before the scored loop ..."
        )
        deadline = time.time() + _SIDECAR_READY_BUDGET
        attempt = 0
        while time.time() < deadline:
            host = _SIDECAR_PROBE_HOSTS[attempt % len(_SIDECAR_PROBE_HOSTS)]
            canon = _canonical(host)
            outfile = f"/tmp/smoke_ready_{attempt}.out"
            _trigger(self.container, host, outfile)
            # A wired sidecar holds the SYN and surfaces a request within ~1s;
            # a fail-close forges the RST, so curl exits at once and no request
            # ever arrives. A short window is enough either way.
            rid = await _wait_for_request(self.app, canon, timeout=5.0)
            if rid is not None:
                self.app._decide_id(rid, DECISION_ALLOWED, DURATION_ONCE)
                await pilot.pause()
                await _wait_resolved(self.app, rid, timeout=15.0)
                print(f"  sidecar wired (probe {host} surfaced a request)\n")
                return
            attempt += 1
            print(
                f"  probe {host}: no request yet (sidecar WS still "
                f"connecting?); retrying ..."
            )
            await asyncio.sleep(0.5)
        raise RuntimeError(
            "sidecar consent WS did not surface a request within "
            f"{_SIDECAR_READY_BUDGET:.0f}s; aborting before the scored loop "
            "would mismatch (#2417)"
        )

    # -- per-iteration -----------------------------------------------------
    async def _run_step(self, pilot, step: _Step) -> _Result:
        canon = _canonical(step.host)
        now = time.time()
        covered, cov_decision, cov_src = self.model.covers(canon, now)
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
                    detail = f"expected a request, none arrived (curl {_exit_label(ec)})"
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
            # Verdict coverage is best-effort: it races with CDN IP rotation
            # and host canonicalization, so a retry resolving to a rotated IP
            # legitimately re-prompts (per #2399 — non-bug). Only allow-list
            # coverage is the deterministic invariant, so reserve a hard
            # MISMATCH for it; a verdict-covered re-prompt is a FINDING,
            # matching the within-retry probe and the model's docstring (#2419).
            soft = expl or cov_src != "allowlist"
            status = FINDING if soft else MISMATCH
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
                    detail=f"expected no request (covered:{cov_decision}:{cov_src}), one arrived",
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
        res.outcome = _outcome_name(res)
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
            + (f" [{r.outcome}]" if r.outcome else "")
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
                    f"expected a re-prompt; none arrived (curl {_exit_label(ec)})",
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
        res = _Result(
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
        res.outcome = _outcome_name(res)
        self.summary.rows.append(res)
        indent = " " * (len(str(self.args.count)) + 3)
        print(
            f"{indent}↳ {label:<20} sidecar={sidecar:<12} "
            f"conn={_exit_label(exit_code):<14} {self._mark(status)}"
            + (f" [{res.outcome}]" if res.outcome else "")
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
        re-prompt). Run under controlled DNS (#2424) so each case is a single
        stable IP: a real host's CDN rotation or long DNS TTL can leave a
        learned ACCEPT rule past a 5s verdict and mask the expiry (the
        allow/deny asymmetry of #2465 -- the allow's DNS-path learn used the
        DNS TTL, so it outlived the verdict; the deny records no such learn).
        SKIPPED (with a note) when controlled DNS is off, since without
        single-IP hosts the exceeding-retry is indeterminate.
        """
        if not self.args.lifecycle:
            return
        if self.dns is None:
            print(
                "\n--- duration lifecycle: SKIPPED (--no-controlled-dns; the "
                "exceeding-retry is indeterminate without single-IP test hosts "
                "-- a real host's CDN rotation / long DNS TTL can keep a timed "
                "allow's learned rule past its verdict, masking the expiry, "
                "#2424/#2465) ---"
            )
            return
        print(
            "\n--- duration lifecycle: within vs exceeding a 5s verdict "
            "(controlled DNS -> hard PASS/FAIL) ---"
        )
        # Single stable IPs (one per case) so the exceeding-retry is
        # deterministic: each controlled name fronts the fixture's :443 HTTPS
        # target (curl -k). The fixture's 60s DNS TTL no longer matters -- the
        # DNS-path learn is capped at the verdict's window (#2465) -- but it is
        # exactly the long-TTL condition that masked the bug on real hosts.
        host_allow, host_deny = "life-allow.test", "life-deny.test"
        self.dns.allocate(host_allow)
        self.dns.allocate(host_deny)
        cases = [
            (host_allow, DECISION_ALLOWED, "allow", EXPECT_RELEASED),
            (host_deny, DECISION_DENIED, "deny", EXPECT_REFUSED),
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

    async def _connect_raw_decider(
        self,
        workspace_id: str,
        attempts: int = 4,
        *,
        ping: bool = False,
        token: str | None = None,
    ) -> RawDecider:
        # Retry a 2nd-decider attach with a generous open timeout so a
        # transient WS-upgrade outage is ridden out rather than failing the
        # multi-decider phase. #2420 (DECIDER2-HANDSHAKE): the 2nd decider's
        # opening handshake has intermittently timed out while the 1st decider
        # + plain HTTP stayed up -- both attempts used to time out (~31s
        # outage), so the default now retries longer (4 x 15s) to ride out a
        # transient while still bounding a *persistent* issue (the caller
        # records a finding instead of hanging the run).
        #
        # Each attempt is timed + printed so the next real occurrence pins
        # whether the stall is client-side (no server response within the
        # open timeout -- correlate with klangkd's per-step
        # "consent decider handshake accepted" log) or server-side. The root
        # cause is still open; klangkd's accept path + event loop + DB were
        # all fast when it did NOT reproduce, so the stall is likely
        # pre-handler (proxy/uvicorn/scheduling) under load.
        # (The prior framing as a TCP-proxy flakiness workaround was
        # disproven -- the proxy is Caddy and reliable, #2398.)
        #
        # workspace_id scopes the decider: a workspace id -> that
        # workspace's consent (needs egress-consent on it). Consent is
        # strictly workspace-scoped (#2976); there is no deploy-wide
        # flavor. Used by the decider-scope phase's isolation probe
        # (#2392), the multi-decider/audit phases, and --with a ``token``
        # override-- the mixed-users phase's second identity (#3112).
        tok = token or self.auth["token"]
        url = f"/ws/consent-decider?token={tok}&workspace={workspace_id}"
        last: Exception | None = None
        for i in range(attempts):
            began = time.time()
            try:
                ws = await ws_connect(self.server, url, open_timeout=15)
                if i:
                    print(
                        f"     (decider attach: attempt {i + 1}/{attempts} "
                        f"succeeded after {time.time() - began:.1f}s)"
                    )
                return RawDecider(ws, ping=ping)
            except Exception as e:  # noqa: BLE001
                last = e
                print(
                    f"     (decider attach: attempt {i + 1}/{attempts} failed "
                    f"after {time.time() - began:.1f}s: {e!r})"
                )
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
            self._shared_d2 = await self._connect_raw_decider(self.ws_id)
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

    # -- mixed-users phase (#3112) --------------------------------------
    async def run_mixed_users_phase(self, pilot) -> None:
        """Two identities decide the SAME workspace at once (#3112).

        The acting identity's textual app plus a raw decider on the OTHER
        identity's token (the seeded member by default; the bootstrap admin
        under --as-member, where the member already acts) are live
        simultaneously, and the phase pins the semantics that must NOT
        change with the users' identity: workspace-scoped (not user-scoped)
        fanout, first-decision-wins and carryover across users, per-user
        ``decided_by`` attribution, and a revoke by the non-deciding user.
        Connections come from the shared workspace container -- consent
        governs the workspace, so every decider of the workspace sees them
        no matter which user connects.
        """
        if not self.args.mixed_users:
            return
        print("\n--- mixed users: two identities, one workspace (#3112) ---")
        d_other: RawDecider | None = None
        try:
            other_auth, other_id = await self._mixed_setup()
            d_other = await self._connect_raw_decider(
                self.ws_id, ping=True, token=other_auth["token"]
            )
            self._extra_deciders.append(d_other)
            await d_other.settle()
            who = "admin" if self.args.as_member else "member"
            print(f"second decider attached on the {who}'s token")
            ok = await self._mixed_visibility(pilot, d_other)
            if ok or self.args.continue_run:
                ok = await self._mixed_late_verdict(pilot, d_other)
            if ok or self.args.continue_run:
                await self._mixed_carryover(pilot, d_other, other_id)
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                _Step(
                    self.summary.total, "(mixed)", "n/a", False, "mixed", "-"
                ),
                "bring up second identity",
                "error",
                None,
                MISMATCH,
                f"mixed-users phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            # CRITICAL: a live second decider keeps the workspace interactive
            # (and would break later phases' premises -- fail-closed,
            # no-decider), so close it eagerly; teardown is a backstop
            # (double-close is a safe no-op).
            if d_other is not None:
                await d_other.close()

    async def _mixed_visibility(self, pilot, d_other: RawDecider) -> bool:
        """1) shared visibility: one off-list SYN surfaces the SAME request
        in both users' deciders; the other user's allow releases it and the
        acting app sees the resolve (cross-user co-decider sync)."""
        host = _MIXED_HOSTS["visibility"]
        canon = _canonical(host)
        step = _Step(
            self.summary.total, host, "domain", False, "mixed-vis", "-"
        )
        outfile = f"/tmp/smoke_mu_v_{self.summary.total}.out"
        _trigger(self.container, host, outfile)
        rid_other = await d_other.wait_for(canon, 12.0)
        rid_app = await _wait_for_request(self.app, canon, 8.0)
        if rid_other is None or rid_app is None or rid_app != rid_other:
            await self._mixed_settle_held(pilot, d_other, rid_app, rid_other)
            ec = _parse_exit(await _wait_result(self.container, outfile))
            self._record_probe(
                step,
                "hold (both users)",
                "no-request(!)",
                ec,
                MISMATCH,
                "not seen by BOTH users' deciders (workspace fanout broken)",
            )
            return False
        await d_other.verdict(rid_other, DECISION_ALLOWED, DURATION_ONCE)
        synced = await _wait_resolved(self.app, rid_other, 15.0)
        ec = _parse_exit(await _wait_result(self.container, outfile))
        if not synced:
            self._record_probe(
                step,
                "other user allows -> app synced",
                "held(!)",
                ec,
                FINDING,
                "the app didn't see the other user's resolve (sidecar hiccup)",
            )
            return True
        status = _classify_conn(EXPECT_RELEASED, ec)
        self._record_probe(
            step,
            "other user allows -> app synced",
            "resolved",
            ec,
            status,
            _mixed_release_detail("other user's allow", status, ec),
        )
        return True

    async def _mixed_late_verdict(self, pilot, d_other: RawDecider) -> bool:
        """2) first-decision-wins ACROSS users, both directions: whichever
        user denies first wins; the other user's late allow on the same
        request must be a no-op -- no rule is installed, so the next
        connection re-prompts."""
        ok = await self._mixed_late_one(
            pilot, d_other, _MIXED_HOSTS["late_other"], by_other=True
        )
        if not ok and not self.args.continue_run:
            return False
        return await self._mixed_late_one(
            pilot, d_other, _MIXED_HOSTS["late_acting"], by_other=False
        )

    async def _mixed_late_one(
        self, pilot, d_other: RawDecider, host: str, *, by_other: bool
    ) -> bool:
        """One first-wins direction: ``by_other`` picks which user's deny is
        first (the other user's late allow/forever must then install
        nothing)."""
        canon = _canonical(host)
        step = _Step(
            self.summary.total, host, "domain", False, "mixed-late", "-"
        )
        outfile = f"/tmp/smoke_mu_l_{self.summary.total}.out"
        _trigger(self.container, host, outfile)
        rid_other = await d_other.wait_for(canon, 12.0)
        rid_app = await _wait_for_request(self.app, canon, 8.0)
        if rid_other is None or rid_app is None:
            await self._mixed_settle_held(pilot, d_other, rid_app, rid_other)
            self._record_probe(
                step,
                "hold",
                "no-request(!)",
                None,
                FINDING,
                "no hold surfaced (sidecar readiness, #2417)",
            )
            return True
        # First verdict (deny/once) from the chosen user.
        if by_other:
            await d_other.verdict(rid_other, DECISION_DENIED, DURATION_ONCE)
        else:
            self.app._decide_id(rid_app, DECISION_DENIED, DURATION_ONCE)
            await pilot.pause()
        await _wait_resolved(self.app, rid_other, 15.0)
        ec1 = _parse_exit(await _wait_result(self.container, outfile))
        if ec1 != EXIT_REFUSED:
            self._record_probe(
                step,
                "first deny (cross-user)",
                "not-refused(!)",
                ec1,
                FINDING,
                f"first cross-user deny not cleanly refused (exit {ec1})",
            )
            return True
        # Late verdict (allow/forever) from the OTHER user: must be a no-op.
        if by_other:
            self.app._decide_id(rid_app, DECISION_ALLOWED, DURATION_FOREVER)
            await pilot.pause()
        else:
            await d_other.verdict(
                rid_other, DECISION_ALLOWED, DURATION_FOREVER
            )
        await asyncio.sleep(0.5)
        # Proof it installed nothing: the next connection must re-prompt
        # (a wrongly-applied forever-allow would connect covered).
        outfile2 = f"/tmp/smoke_mu_l2_{self.summary.total}.out"
        _trigger(self.container, host, outfile2)
        rid2 = await _wait_for_request(self.app, canon, timeout=12.0)
        if rid2 is None:
            ec2 = _parse_exit(
                await _wait_result(self.container, outfile2, timeout=10.0)
            )
            if ec2 == 0:
                self._record_probe(
                    step,
                    "late verdict no-op",
                    "covered(!)",
                    ec2,
                    MISMATCH,
                    "late cross-user verdict took effect (re-prompt missing)",
                )
                return False
            self._record_probe(
                step,
                "late verdict no-op",
                "no-reprompt(!)",
                ec2,
                FINDING,
                "no re-prompt after the late verdict (hung / NFQUEUE-DNS)",
            )
            return True
        self.app._decide_id(rid2, DECISION_ALLOWED, DURATION_ONCE)
        await pilot.pause()
        await _wait_resolved(self.app, rid2, 15.0)
        ec2 = _parse_exit(await _wait_result(self.container, outfile2))
        self._record_probe(
            step,
            "late verdict no-op",
            "re-prompt",
            ec2,
            PASS,
            "first decision won; the late cross-user verdict "
            "installed no rule",
        )
        return True

    async def _mixed_settle_held(
        self,
        pilot,
        d_other: RawDecider,
        rid_app: str | None,
        rid_other: str | None,
    ) -> None:
        """Release a half-seen hold so it cannot dangle: allow/once from
        whichever side holds it (best-effort, both may be None)."""
        if rid_app is not None:
            self.app._decide_id(rid_app, DECISION_ALLOWED, DURATION_ONCE)
            await pilot.pause()
        elif rid_other is not None:
            await d_other.verdict(rid_other, DECISION_ALLOWED, DURATION_ONCE)

    async def _mixed_carryover(
        self, pilot, d_other: RawDecider, other_id: str | None
    ) -> None:
        """3) a carrying verdict by the OTHER user covers later connections
        regardless of decider (workspace-scoped, not decider-scoped); the
        egress_rules row names that user (``decided_by``); and the ACTING
        user can revoke it (cross-user revoke -> re-prompt)."""
        host = _MIXED_HOSTS["carryover"]
        canon = _canonical(host)
        step = _Step(
            self.summary.total, host, "domain", False, "mixed-carry", "-"
        )
        outfile = f"/tmp/smoke_mu_c_{self.summary.total}.out"
        _trigger(self.container, host, outfile)
        rid_other = await d_other.wait_for(canon, 12.0)
        rid_app = await _wait_for_request(self.app, canon, 8.0)
        if rid_other is None or rid_app is None:
            await self._mixed_settle_held(pilot, d_other, rid_app, rid_other)
            self._record_probe(
                step,
                "hold",
                "no-request(!)",
                None,
                FINDING,
                "no hold surfaced (sidecar readiness, #2417)",
            )
            return
        # The other user's 1h allow: carries, and its row must name them.
        await d_other.verdict(rid_other, DECISION_ALLOWED, DURATION_1H)
        await _wait_resolved(self.app, rid_other, 15.0)
        ec = _parse_exit(await _wait_result(self.container, outfile))
        await self._mixed_check_attribution(d_other, step, canon, other_id, ec)
        # Carryover: the next connection is covered (no request anywhere).
        outfile2 = f"/tmp/smoke_mu_c2_{self.summary.total}.out"
        _trigger(self.container, host, outfile2)
        stray = await _wait_no_request(self.app, canon, window=6.0)
        stray_other = await d_other.wait_no_request(canon, window=6.0)
        ec2 = _parse_exit(await _wait_result(self.container, outfile2))
        if stray is not None or stray_other is not None:
            await self._mixed_settle_held(pilot, d_other, stray, stray_other)
            self._record_probe(
                step,
                "cross-user carryover",
                "request(!)",
                ec2,
                MISMATCH,
                "cross-user 1h allow did not cover the retry "
                "(verdict is decider-scoped?)",
            )
            return
        carry_status = _classify_conn(EXPECT_RELEASED, ec2)
        self._record_probe(
            step,
            "cross-user carryover",
            "no-req",
            ec2,
            carry_status,
            _mixed_release_detail(
                "cross-user covered allow", carry_status, ec2
            ),
        )
        # Cross-user revoke: the ACTING user drops the other user's verdict.
        try:
            if self.app._ws is not None:
                await self.app._ws.send(make_revoke(rid_other))
        except Exception:
            pass
        await pilot.pause()
        await asyncio.sleep(2.0)  # let the server + sidecar process the drop
        outfile3 = f"/tmp/smoke_mu_c3_{self.summary.total}.out"
        _trigger(self.container, host, outfile3)
        rid3 = await _wait_for_request(self.app, canon, timeout=12.0)
        if rid3 is None:
            ec3 = _parse_exit(
                await _wait_result(self.container, outfile3, timeout=10.0)
            )
            self._record_probe(
                step,
                "cross-user revoke",
                "no-reprompt(!)",
                ec3,
                FINDING if ec3 is None else MISMATCH,
                "post-revoke connection hung (NFQUEUE/DNS)"
                if ec3 is None
                else "revoke by the non-deciding user did not drop the "
                "rule (still covered)",
            )
            return
        self.app._decide_id(rid3, DECISION_ALLOWED, DURATION_ONCE)
        await pilot.pause()
        await _wait_resolved(self.app, rid3, 15.0)
        ec3 = _parse_exit(await _wait_result(self.container, outfile3))
        self._record_probe(
            step,
            "cross-user revoke",
            "re-prompt",
            ec3,
            PASS,
            "the acting user's revoke dropped the other user's verdict",
        )

    async def _mixed_check_attribution(
        self,
        d_other: RawDecider,
        step: _Step,
        canon: str,
        other_id: str | None,
        ec: int | None,
    ) -> None:
        """The refreshed egress_rules row for a verdict must name the user
        who decided it (``decided_by``), not the run's acting identity."""
        row = await d_other.wait_rule(canon, 12.0)
        if row is None:
            self._record_probe(
                step,
                "audit decided_by",
                "no-rules(!)",
                ec,
                FINDING,
                "no egress_rules frame carried the verdict row",
            )
            return
        if other_id is None:
            self._record_probe(
                step,
                "audit decided_by",
                "no-id(!)",
                ec,
                FINDING,
                "user id lookup missed; cannot check attribution",
            )
            return
        by = row.get("decided_by")
        if by == other_id and row.get("decision") == DECISION_ALLOWED:
            self._record_probe(
                step, "audit decided_by", "other-user", ec, PASS, ""
            )
        else:
            self._record_probe(
                step,
                "audit decided_by",
                str(by),
                ec,
                MISMATCH,
                f"decided_by={by!r} decision={row.get('decision')!r} "
                f"(expected the OTHER user's id)",
            )

    async def run_snapshot_replay_phase(self, pilot) -> None:
        """Reconnect snapshot replay: a request resolved while a decider is
        disconnected does NOT replay in the snapshot it gets on reconnect; a
        still-held one does.

        Default-ON under controlled DNS (#2424): with a single stable IP per
        host there is no CDN IP-cascade respawn to masquerade as a replay
        event, so the phase is a hard PASS/FAIL rather than the indeterminate
        FINDING it was on real multi-IP hosts. Skipped (with a note) when
        controlled DNS is off, since without it the result is indeterminate."""
        if not self.args.snapshot:
            return
        if self.dns is None:
            print(
                "\n--- reconnect snapshot replay: SKIPPED "
                "(--no-controlled-dns; the result is indeterminate without "
                "single-IP test hosts, #2424) ---"
            )
            return
        print(
            "\n--- reconnect snapshot replay: resolved-while-away rows don't "
            "replay (controlled DNS -> hard PASS/FAIL) ---"
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
        # Controlled, single-IP hosts (distinct IPs so A and B are two distinct
        # consent flows; each has exactly one IP, so denying A can't cascade into
        # a fresh held request that looks like a replay bug).
        host_a, host_b = "snap-a.test", "snap-b.test"
        self.dns.allocate_pair(host_a, host_b)
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
            # Controlled DNS -> A has a single IP -> a deny can't cascade into a
            # fresh held request. So A present in the reconnect snapshot IS a
            # real replay bug (a resolved-while-away row replayed), not timing.
            status, detail = (
                MISMATCH,
                "A replayed in reconnect snapshot (resolved-while-away row "
                "replayed; a real snapshot bug, not a CDN cascade under "
                "controlled DNS)",
            )
        else:
            status, detail = (
                MISMATCH,
                f"B missing from reconnect snapshot (a still-held row "
                f"vanished): B={has_b} A={has_a}",
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

    async def run_fanout_phase(self, pilot) -> None:
        """Per-connection verdict cache + N-way concurrent fan-out (#2441).

        Three deterministic assertions on the real stack, each a hard
        PASS/FAIL under controlled DNS:

        1. **N-way fan-out** -- :data:`_FANOUT_N` concurrent off-list connects
           to :data:`_FANOUT_N` DISTINCT hosts surface :data:`_FANOUT_N`
           DISTINCT consent requests, all held SIMULTANEOUSLY (not serialized
           behind the first). The post-#2331 loop-driven NFQUEUE consumer holds
           N flows concurrently; the pre-#2331 blocking consumer reported only
           the first until it resolved (#2336/#2337).
        2. **concurrent same-host** (#2361) -- a ``once`` verdict on one
           connection does NOT satisfy a separate concurrent connection to the
           SAME host. The consent *coordinator* dedupes a second concurrent
           request for the same ``(workspace, dst, dport)`` to a fail-closed
           DENY (``consent_coordinator.hold`` -> ``create_request`` returns
           None -> duplicate), so two concurrent curls to a single-IP host
           surface ONE prompt and the duplicate SYN is refused (not allowed
           through by the held request's eventual verdict). Pre-#2366 the
           sidecar ``(dst, port)`` cache was a separate, fixed leak path; the
           sequential re-prompt under ``once`` it gated is asserted by the
           scored loop's exceeding-retry(once) probe + #2366's e2e.
        3. **retransmit dedup** (#2366) -- a held connection surfaces exactly
           ONE request, not one per SYN retransmit. The in-flight set dedupes a
           held connection's retransmits (same 4-tuple -> drop), so holding
           past several retransmit intervals produces no duplicate request.

        Needs controlled DNS (#2424): #2/#3 need a single stable IP per host so
        the only thing distinguishing two concurrent connections is the source
        port (the connection 4-tuple), and a held connection's retransmits
        share it. Skipped otherwise (indeterminate on real multi-IP hosts).

        Driven over the hero workspace's own textual decider (``self.app``);
        ``controller.pending`` is read directly, so no 2nd-decider WS (and its
        #2420 handshake flakiness) is needed.
        """
        if not self.args.fanout:
            return
        if self.dns is None:
            print(
                "\n--- per-connection verdict cache / fan-out: SKIPPED "
                "(--no-controlled-dns; #2/#3 need a single stable IP so the "
                "only difference between two concurrent connections is the "
                "source port, #2424) ---"
            )
            return
        print(
            f"\n--- per-connection verdict cache: {_FANOUT_N}-way fan-out + "
            "once-not-shared + retransmit-dedup (#2361/#2366/#2337) ---"
        )
        parent = _Step(
            self.summary.total, "fan-out", "n/a", False, "fan-out", "-"
        )
        app = self.app
        try:
            # -- 1) N-way fan-out: N distinct hosts -> N distinct requests, all
            #       held simultaneously (#2337). Each controlled name gets its
            #       own stable IP so the L3/L4 rule can't merge two hosts.
            fan_hosts = [f"fan-{i}.test" for i in range(_FANOUT_N)]
            for h in fan_hosts:
                self.dns.allocate(h)
            fan_canons = [_canonical(h) for h in fan_hosts]
            fan_outs = []
            for i, h in enumerate(fan_hosts):
                of = f"/tmp/smoke_fo_d{i}.out"
                fan_outs.append(of)
                _trigger(self.container, h, of)
            present = await _wait_all_hosts(app, fan_canons, timeout=15.0)
            if present is None:
                got = [c for c in fan_canons if _pending_rids_for(app, c)]
                self._record_probe(
                    parent,
                    f"{_FANOUT_N}-way fan-out",
                    "no-request(!)",
                    None,
                    MISMATCH,
                    f"fan-out: only {len(got)} of {_FANOUT_N} hosts surfaced "
                    f"a request simultaneously (serialized? #2336/#2337)",
                )
                if not self.args.continue_run:
                    self._abort = True
                return
            rids = [present[c] for c in fan_canons]
            distinct = len(set(rids)) == _FANOUT_N
            held = sum(rid in app.controller.pending for rid in rids)
            if distinct and held == _FANOUT_N:
                self._record_probe(
                    parent,
                    f"{_FANOUT_N}-way fan-out",
                    f"{_FANOUT_N} held",
                    None,
                    PASS,
                    f"{_FANOUT_N} distinct hosts -> {_FANOUT_N} distinct "
                    f"requests, all held simultaneously (#2337)",
                )
            else:
                self._record_probe(
                    parent,
                    f"{_FANOUT_N}-way fan-out",
                    f"distinct={len(set(rids))},held={held}",
                    None,
                    MISMATCH,
                    f"fan-out: not {_FANOUT_N} distinct simultaneous requests "
                    f"(distinct={distinct}, held={held})",
                )
                if not self.args.continue_run:
                    self._abort = True
                    return
            # Release each with once (no IP learn -> can't perturb later
            # probes) and drain the curls so none dangle.
            for rid in rids:
                if rid in app.controller.pending:
                    app._decide_id(rid, DECISION_ALLOWED, DURATION_ONCE)
            await pilot.pause()
            for rid in rids:
                await _wait_resolved(app, rid, timeout=15.0)
            for of in fan_outs:
                await _wait_result(self.container, of, timeout=15.0)
            if self._abort:
                return

            # -- 2) concurrent same-host: the consent COORDINATOR dedupes a
            #       second concurrent request for the same (workspace, dst,
            #       dport) to a fail-closed DENY (consent_coordinator.hold ->
            #       create_request returns None -> duplicate). So two concurrent
            #       curls to a single-IP host surface ONE prompt; the duplicate
            #       SYN is refused, NOT satisfied by the held request's eventual
            #       verdict. That is bullet #2's claim -- a `once` on one
            #       connection does NOT satisfy a separate concurrent connection
            #       to the same host (#2361): the separate connection is denied,
            #       not allowed through. (The sidecar's per-connection verdict
            #       cache that makes a *sequential* retry re-prompt under `once`
            #       is asserted by the scored loop's exceeding-retry(once) probe
            #       + #2366's e2e; the CONCURRENT case is the coordinator
            #       per-destination dedup asserted here.)
            same_host = "fan-same.test"
            self.dns.allocate(same_host)
            csame = _canonical(same_host)
            of_s1 = "/tmp/smoke_fo_s1.out"
            of_s2 = "/tmp/smoke_fo_s2.out"
            _trigger(self.container, same_host, of_s1)
            _trigger(self.container, same_host, of_s2)
            # Let both SYNs reach the coordinator, then read the steady-state
            # request count for this host (1 == deduped; >=2 == dedup broken).
            await asyncio.sleep(2.0)
            same_rids = _pending_rids_for(app, csame)
            n_same = len(same_rids)
            if n_same == 0:
                await _wait_result(self.container, of_s1, timeout=8.0)
                await _wait_result(self.container, of_s2, timeout=8.0)
                self._record_probe(
                    parent,
                    "once cross-connection",
                    "no-request(!)",
                    None,
                    FINDING,
                    "no hold surfaced for the concurrent same-host probe "
                    "(sidecar readiness?)",
                )
            elif n_same >= 2:
                # dedup broken: settle the pile-up, then move on.
                for rid in same_rids:
                    app._decide_id(rid, DECISION_DENIED, DURATION_ONCE)
                await pilot.pause()
                for rid in same_rids:
                    await _wait_resolved(app, rid, timeout=10.0)
                await _wait_result(self.container, of_s1, timeout=10.0)
                await _wait_result(self.container, of_s2, timeout=10.0)
                self._record_probe(
                    parent,
                    "once cross-connection",
                    f"{n_same} prompts(!)",
                    None,
                    MISMATCH,
                    f"concurrent same-host connections produced {n_same} "
                    f"prompts (coordinator per-destination dedup broken)",
                )
                if not self.args.continue_run:
                    self._abort = True
            else:
                # n_same == 1: the coordinator deduped to one prompt. Allow it
                # with once -> the DECIDING connection is released; the
                # DUPLICATE was already denied fail-closed. Assert exactly one
                # curl succeeded and the other was refused (not satisfied).
                rid = same_rids[0]
                app._decide_id(rid, DECISION_ALLOWED, DURATION_ONCE)
                await pilot.pause()
                await _wait_resolved(app, rid, timeout=15.0)
                ec_s1 = _parse_exit(
                    await _wait_result(self.container, of_s1, timeout=15.0)
                )
                ec_s2 = _parse_exit(
                    await _wait_result(self.container, of_s2, timeout=15.0)
                )
                if (ec_s1 == 0) ^ (ec_s2 == 0):
                    self._record_probe(
                        parent,
                        "once cross-connection",
                        "deduped+denied",
                        ec_s1,
                        PASS,
                        "concurrent same-host deduped to one prompt; the "
                        "duplicate was fail-closed denied, the deciding "
                        "connection released by once -- the separate "
                        "connection was NOT satisfied (#2361)",
                    )
                elif ec_s1 == 0 and ec_s2 == 0:
                    self._record_probe(
                        parent,
                        "once cross-connection",
                        "leaked(!)",
                        ec_s1,
                        MISMATCH,
                        "a once verdict on one connection released a separate "
                        "concurrent connection to the same host (#2361)",
                    )
                    if not self.args.continue_run:
                        self._abort = True
                else:
                    # Both refused: the duplicate's destination-scoped
                    # fail-close REJECT (reject(dst,port,...) in proxy.py)
                    # clobbered the deciding connection's allow too. The
                    # duplicate was still NOT satisfied (denied, not allowed
                    # through) -- bullet #2's claim holds -- but the deciding
                    # connection's release was collateral damage. A known
                    # concurrent-same-host edge case -> finding, not a hard fail.
                    self._record_probe(
                        parent,
                        "once cross-connection",
                        "both refused",
                        ec_s1,
                        FINDING,
                        "concurrent same-host deduped to one prompt, but "
                        "both connections refused -- the duplicate's "
                        "destination-scoped fail-close REJECT clobbered the "
                        "deciding connection's allow; the separate "
                        "connection was still NOT satisfied (#2361)",
                    )
            if self._abort:
                return

            # -- 3) retransmit dedup: a held connection surfaces exactly ONE
            #       request, not one per SYN retransmit (#2366). Hold past
            #       several retransmit intervals WITHOUT verdicting; the
            #       in-flight set drops the retransmits (same 4-tuple), so the
            #       count stays at 1.
            rt_host = "fan-rt.test"
            self.dns.allocate(rt_host)
            crt = _canonical(rt_host)
            of_rt = "/tmp/smoke_fo_rt.out"
            _trigger(self.container, rt_host, of_rt)
            rt_rid = await _wait_for_request(app, crt, timeout=12.0)
            if rt_rid is None:
                self._record_probe(
                    parent,
                    "retransmit dedup",
                    "no-request(!)",
                    None,
                    FINDING,
                    "no hold surfaced for the retransmit probe "
                    "(sidecar readiness?)",
                )
                return
            # Stay under the consent timeout so the server doesn't auto-deny
            # mid-window; a few seconds covers 2-3 SYN retransmits (RTO ~1s).
            rt_window = min(5.0, max(2.0, self.args.consent_timeout - 4.0))
            before = len(_pending_rids_for(app, crt))
            await asyncio.sleep(rt_window)
            after_rids = _pending_rids_for(app, crt)
            after = len(after_rids)
            # Release it (allow/once) before the consent timeout auto-denies.
            app._decide_id(rt_rid, DECISION_ALLOWED, DURATION_ONCE)
            await pilot.pause()
            await _wait_resolved(app, rt_rid, timeout=15.0)
            ec_rt = _parse_exit(
                await _wait_result(self.container, of_rt, timeout=15.0)
            )
            if before == 1 and after == 1:
                self._record_probe(
                    parent,
                    "retransmit dedup",
                    "1 request",
                    ec_rt,
                    PASS,
                    f"held {rt_window:.0f}s (past SYN retransmits) -> still "
                    f"exactly one request; retransmits deduped (#2366)",
                )
            else:
                for rid in after_rids:
                    if rid in app.controller.pending and rid != rt_rid:
                        app._decide_id(rid, DECISION_DENIED, DURATION_ONCE)
                await pilot.pause()
                for rid in after_rids:
                    if rid != rt_rid:
                        await _wait_resolved(app, rid, timeout=10.0)
                self._record_probe(
                    parent,
                    "retransmit dedup",
                    f"{after} requests(!)",
                    ec_rt,
                    MISMATCH,
                    f"a held connection produced {after} request(s) after "
                    f"{rt_window:.0f}s (retransmit dedup broken, #2366)",
                )
                if not self.args.continue_run:
                    self._abort = True
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                parent,
                "fan-out phase",
                "error",
                None,
                MISMATCH,
                f"fan-out phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True

    async def _raw_wait_no_request(
        self, d: RawDecider, canon: str, window: float = 5.0
    ) -> str | None:
        """None if no request for ``canon`` surfaces in ``window`` (draining
        frames so a late egress_request is actually seen), else the request id.
        Mirrors :func:`_wait_no_request` for the textual app, for a RawDecider."""
        deadline = time.time() + window
        while time.time() < deadline:
            rid = next((r for r, h in d.held.items() if h == canon), None)
            if rid is not None:
                return rid
            await d._recv(0.3)
        return None

    async def _raw_wait_for_any(
        self, d: RawDecider, canons: set[str], timeout: float = 12.0
    ) -> str | None:
        """The request id of the first held request whose canonical dest is in
        ``canons``, or None on timeout. Used by the port-scope phase, where an
        off-port request to an allow-listed host surfaces named by IP (the
        allow-list DNS-learn path leaves the record's host=None), so the request
        matches the controlled IP, not the hostname."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rid = next((r for r, h in d.held.items() if h in canons), None)
            if rid is not None:
                return rid
            await d._recv(0.4)
        return None

    async def _probe_sidecar_ready_raw(
        self, d: RawDecider, cont: str, host: str, budget: float = 60.0
    ) -> bool:
        """True once a throwaway off-list host surfaces a consent request in
        ``d`` (the sidecar consent-WS is wired), else False. Allows the hold so
        the probe can't perturb later assertions. Mirrors _wait_sidecar_ready
        for a workspace driven via a RawDecider (#2417) -- a fresh workspace's
        sidecar WS connects on its own backoff, so a missing prompt for the
        FIRST scored host is readiness, not a scope violation."""
        canon = _canonical(host)
        deadline = time.time() + budget
        attempt = 0
        while time.time() < deadline:
            of = f"/tmp/smoke_rawready_{attempt}.out"
            _trigger(cont, host, of)
            rid = await d.wait_for(canon, timeout=6.0)
            if rid is not None:
                await d.verdict(rid, DECISION_ALLOWED, DURATION_ONCE)
                await d.wait_resolution(rid, 15.0)
                return True
            attempt += 1
            await asyncio.sleep(0.5)
        return False

    async def run_host_scope_phase(self, pilot) -> None:
        """nginx-style host scopes (#2377) against distinct controlled IPs (#2424).

        With the apex and its subdomain on DIFFERENT controlled IPs, the L3/L4
        allow rule (per IP) can't paper over the L7 hostname spec, so each scope
        mode is observable on the real stack:
          * bare ``exact.test`` (EXACT): apex covered, subdomain NOT.
          * ``.incl.test`` (INCLUSIVE): apex + subdomains covered, including
            a *multi-level* subdomain (``a.b.incl.test``) -- pins the
            ``endswith('.host')`` boundary at depth > 1, not just one label.
          * ``*.wild.test`` (SUBDOMAINS): subdomains covered (incl. deep),
            apex NOT.
        Driven via a dedicated interactive workspace + a raw decider scoped to
        it. Needs controlled DNS (skipped otherwise)."""
        if not self.args.host_scope:
            return
        if self.dns is None:
            print(
                "\n--- host scope: SKIPPED (--no-controlled-dns; needs distinct "
                "controlled IPs so the L3/L4 rule can't mask the L7 spec, #2424) ---"
            )
            return
        print(
            "\n--- host scope: exact / inclusive / subdomains (#2377) on "
            "distinct controlled IPs ---"
        )
        parent = _Step(
            self.summary.total, "host-scope", "n/a", False, "host-scope", "-"
        )
        # Distinct controlled IPs per name. The (apex, sub) pairs MUST differ so
        # an allow-learned ACCEPT for the apex IP can't cover the subdomain's IP.
        cases = [
            ("exact.test", True, "exact(apex) covered"),
            ("sub.exact.test", False, "exact(sub) NOT covered"),
            ("incl.test", True, "inclusive(apex) covered"),
            ("sub.incl.test", True, "inclusive(sub) covered"),
            # Multi-level subdomain (#2442): ``.host`` is endswith('.host'),
            # so a >1-label-deep name must also be covered. Asserted here (not
            # just in the model) at the sidecar DNS/consent layer via its own
            # distinct controlled IP.
            ("a.b.incl.test", True, "inclusive(deep sub) covered"),
            ("wild.test", False, "subdomains(apex) NOT covered"),
            ("sub.wild.test", True, "subdomains(sub) covered"),
            # Symmetric deep-sub assertion for ``*.host`` (same endswith branch).
            ("a.b.wild.test", True, "subdomains(deep sub) covered"),
        ]
        for host, _covered, _label in cases:
            self.dns.allocate(host)
        allow = ["exact.test", ".incl.test", "*.wild.test"]
        ws_id = None
        d: RawDecider | None = None
        cont: str | None = None
        conn = None
        try:
            ws_id = await self._new_workspace(
                allow,
                [],
                f"smoke-hostscope-{int(time.time() * 1000) % 100000}",
            )
            self._extra_ws_ids.append(ws_id)
            print(f"host-scope workspace {ws_id}  allow={allow}")
            conn = await self._open_terminal_ws(ws_id)
            self._extra_ws_conns.append(
                (conn, asyncio.create_task(self._drain(conn)))
            )
            cont = _container_for_workspace(ws_id)
            d = await self._connect_raw_decider(ws_id, ping=True)
            self._extra_deciders.append(d)
            await d.settle()
            # Wait for the sidecar consent-WS to wire before the scored hosts
            # (#2417): a fresh workspace's sidecar connects on its own backoff,
            # so the first non-covered host could fail-close (no prompt) and
            # false-positive as a scope violation.
            ready_host = "hs-ready.test"
            self.dns.allocate(ready_host)
            if not await self._probe_sidecar_ready_raw(d, cont, ready_host):
                self._record_probe(
                    parent,
                    "sidecar ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "host-scope phase failed: sidecar consent-WS did not wire "
                    "(#2417); cannot assert scope boundaries",
                )
                await d.close()
                return

            for host, expect_covered, label in cases:
                if self._abort:
                    return
                canon = _canonical(host)
                of = f"/tmp/smoke_hs_{canon.replace('.', '_')}.out"
                _trigger(cont, host, of)
                if expect_covered:
                    # Covered -> no consent request, and the connection succeeds
                    # (the learned ACCEPT + the test server -> exit 0).
                    rid = await self._raw_wait_no_request(d, canon, window=5.0)
                    text = await _wait_result(cont, of, timeout=20.0)
                    ec = _parse_exit(text)
                    if rid is None and ec == 0:
                        sx, dx = PASS, ""
                    elif rid is not None:
                        sx, dx = (
                            MISMATCH,
                            (
                                f"host scope: {label} -- {host} prompted for consent "
                                f"(should be covered by the allow-list)"
                            ),
                        )
                    else:
                        sx, dx = (
                            FINDING,
                            (
                                f"host scope: {label} -- covered host did not connect "
                                f"cleanly (exit {ec})"
                            ),
                        )
                    self._record_probe(parent, label, "covered", ec, sx, dx)
                else:
                    # NOT covered -> a consent request surfaces; allow it so the
                    # connection completes (exit 0) and clean up.
                    rid = await d.wait_for(canon, 12.0)
                    if rid is None:
                        self._record_probe(
                            parent,
                            label,
                            "no-request(!)",
                            None,
                            MISMATCH,
                            f"host scope: {label} -- {host} did NOT prompt "
                            f"(should be uncovered by the allow-list)",
                        )
                        if not self.args.continue_run:
                            self._abort = True
                        return
                    await d.verdict(rid, DECISION_ALLOWED, DURATION_ONCE)
                    await d.wait_resolution(rid, 15.0)
                    text = await _wait_result(cont, of)
                    ec = _parse_exit(text)
                    if ec == 0:
                        sx, dx = PASS, ""
                    else:
                        sx, dx = (
                            FINDING,
                            (
                                f"host scope: {label} -- allowed but did not connect "
                                f"cleanly (exit {ec})"
                            ),
                        )
                    self._record_probe(
                        parent, label, "prompted+allow", ec, sx, dx
                    )
                if sx == MISMATCH and not self.args.continue_run:
                    self._abort = True
                    return
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                parent,
                "bring up host-scope ws",
                "error",
                None,
                MISMATCH,
                f"host-scope phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            if d is not None:
                await d.close()

    async def run_port_scope_phase(self, pilot) -> None:
        """Port-scoped allow specs (#2377 / #2256) on a controlled IP (#2424).

        ``svc.test:443`` allow-lists ONLY :443; ``:80`` on the same controlled
        IP must still prompt. Same IP (the port scoping is at L4, so the L3/L4
        rule is per IP+port and the confound the host-scope phase works around
        does not apply). Needs controlled DNS (skipped otherwise)."""
        if not self.args.port_scope:
            return
        if self.dns is None:
            print(
                "\n--- port scope: SKIPPED (--no-controlled-dns; needs a "
                "controlled IP with a test server on :80 + :443, #2424) ---"
            )
            return
        print(
            "\n--- port scope: host:443 does not permit :80 on the same "
            "controlled IP ---"
        )
        parent = _Step(
            self.summary.total, "port-scope", "n/a", False, "port-scope", "-"
        )
        self.dns.allocate("svc.test")
        allow = ["svc.test:443"]
        ws_id = None
        d: RawDecider | None = None
        cont: str | None = None
        conn = None
        try:
            ws_id = await self._new_workspace(
                allow,
                [],
                f"smoke-portscope-{int(time.time() * 1000) % 100000}",
            )
            self._extra_ws_ids.append(ws_id)
            print(f"port-scope workspace {ws_id}  allow={allow}")
            conn = await self._open_terminal_ws(ws_id)
            self._extra_ws_conns.append(
                (conn, asyncio.create_task(self._drain(conn)))
            )
            cont = _container_for_workspace(ws_id)
            d = await self._connect_raw_decider(ws_id, ping=True)
            self._extra_deciders.append(d)
            await d.settle()
            ready_host = "ps-ready.test"
            self.dns.allocate(ready_host)
            if not await self._probe_sidecar_ready_raw(d, cont, ready_host):
                self._record_probe(
                    parent,
                    "sidecar ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "port-scope phase failed: sidecar consent-WS did not wire "
                    "(#2417); cannot assert port scoping",
                )
                await d.close()
                return

            # 1) :443 is covered -> no prompt, connect OK (exit 0).
            of443 = "/tmp/smoke_ps_443.out"
            _trigger(cont, "svc.test", of443, port=443, scheme="https")
            rid = await self._raw_wait_no_request(
                d, _canonical("svc.test"), window=5.0
            )
            text = await _wait_result(cont, of443, timeout=20.0)
            ec = _parse_exit(text)
            if rid is None and ec == 0:
                sx, dx = PASS, ""
            elif rid is not None:
                sx, dx = (
                    MISMATCH,
                    ("port scope: svc.test:443 prompted (should be covered)"),
                )
            else:
                sx, dx = (
                    FINDING,
                    (
                        f"port scope: :443 covered host did not connect cleanly (exit {ec})"
                    ),
                )
            self._record_probe(parent, ":443 covered", "covered", ec, sx, dx)
            if sx == MISMATCH and not self.args.continue_run:
                self._abort = True
                await d.close()
                return

            # 2) :80 is NOT covered -> a prompt surfaces; allow + clean up.
            # NOTE: the request is named by IP, not hostname. svc.test IS
            # allow-listed (on :443), so the proxy's allow-list DNS-learn path
            # (allow(), which leaves the learned record's host=None) recorded
            # the IP without a host name -- only the non-allow-listed
            # _record_hosts path names the host. So this off-port request
            # surfaces as the controlled IP, not "svc.test". Port-scoping itself
            # is correct (:80 is not covered -> prompts); match either name.
            svc_ip = self.dns.ip_for("svc.test")
            of80 = "/tmp/smoke_ps_80.out"
            _trigger(cont, "svc.test", of80, port=80, scheme="http")
            rid = await self._raw_wait_for_any(
                d, {_canonical("svc.test"), _canonical(svc_ip)}, 12.0
            )
            if rid is None:
                self._record_probe(
                    parent,
                    ":80 NOT covered",
                    "no-request(!)",
                    None,
                    MISMATCH,
                    "port scope: svc.test:80 did NOT prompt (host:443 leaked to :80)",
                )
                if not self.args.continue_run:
                    self._abort = True
                await d.close()
                return
            await d.verdict(rid, DECISION_ALLOWED, DURATION_ONCE)
            await d.wait_resolution(rid, 15.0)
            text = await _wait_result(cont, of80)
            ec = _parse_exit(text)
            if ec == 0:
                sx, dx = PASS, ""
            else:
                sx, dx = (
                    FINDING,
                    (
                        f"port scope: :80 allowed but did not connect cleanly (exit {ec})"
                    ),
                )
            self._record_probe(
                parent, ":80 prompted+allow", "prompted+allow", ec, sx, dx
            )
            if sx == MISMATCH and not self.args.continue_run:
                self._abort = True
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                parent,
                "bring up port-scope ws",
                "error",
                None,
                MISMATCH,
                f"port-scope phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            if d is not None:
                await d.close()

    async def run_coresident_phase(self, pilot) -> None:
        """Pin co-resident-host behavior as a canary for #2352 (#2440).

        Per-host allow/revoke is L3/L4-only today: two hostnames that
        resolve to the SAME IP share one iptables rule, so allowing host A
        also lets host B through (#2352, OPEN). This phase points two
        controlled hostnames at one IP and pins both halves of that
        limitation as canaries:

          1. Allow A (learn a per-IP ACCEPT) -> B (same IP) connects with NO
             prompt (the per-IP allow leak).
          2. Revoke A -> B STILL connects with no re-prompt (the revoke is a
             NO-op). Canary #1's B connect resolves B after A's allow, and
             ``_record_hosts`` keeps only the LATEST name per IP, so
             ``LEARNED[ip]["host"]`` is ``core-b`` when
             ``drop_for_host("core-a")`` scans it: no rule is dropped and
             both hosts stay reachable (the under-removal half of #2352;
             the over-removal half -- revoke A of the name still on record
             uncovers B -- needs B to NOT resolve in between, which this
             sequence cannot produce).

        Both assertions are CANARIES: they PASS while #2352 is unfixed and
        FLIP to MISMATCH when per-host allow/revoke lands, so a silent fix
        can't go unnoticed. Driven via a dedicated interactive workspace + a
        raw decider scoped to it. Needs controlled DNS (skipped otherwise).
        """
        if not self.args.coresident:
            return
        if self.dns is None:
            print(
                "\n--- co-resident hosts: SKIPPED (--no-controlled-dns; needs "
                "two names on one controlled IP, #2424/#2352) ---"
            )
            return
        print(
            "\n--- co-resident hosts: pin per-IP allow/revoke sharing "
            "(canary for #2352) ---"
        )
        parent = _Step(
            self.summary.total, "coresident", "n/a", False, "coresident", "-"
        )
        # Two names on ONE controlled IP (#2352's premise). co_locate() maps B
        # to A's IP rather than allocating a fresh one, so they share the
        # sidecar's single per-IP ACCEPT/REJECT rule.
        host_a, host_b = "core-a.test", "core-b.test"
        ip_a = self.dns.allocate(host_a)
        self.dns.co_locate(host_b, host_a)
        assert self.dns.ip_for(host_b) == ip_a, (
            "co-resident hosts must share an IP"
        )
        allow: list[str] = []  # neither off-list -> both prompt
        ws_id = None
        d: RawDecider | None = None
        cont: str | None = None
        conn = None
        try:
            ws_id = await self._new_workspace(
                allow,
                [],
                f"smoke-coresident-{int(time.time() * 1000) % 100000}",
            )
            self._extra_ws_ids.append(ws_id)
            print(
                f"co-resident workspace {ws_id}  "
                f"{host_a}+{host_b}->{ip_a}  allow=[] (both off-list)"
            )
            conn = await self._open_terminal_ws(ws_id)
            self._extra_ws_conns.append(
                (conn, asyncio.create_task(self._drain(conn)))
            )
            cont = _container_for_workspace(ws_id)
            d = await self._connect_raw_decider(ws_id, ping=True)
            self._extra_deciders.append(d)
            await d.settle()
            # Wait for the sidecar consent-WS to wire before the scored holds
            # (#2417): a fresh workspace's sidecar connects on its own
            # backoff, so the first off-list hold could fail-close (no prompt)
            # and false-fire the canary.
            ready_host = "core-ready.test"
            self.dns.allocate(ready_host)
            if not await self._probe_sidecar_ready_raw(d, cont, ready_host):
                self._record_probe(
                    parent,
                    "sidecar ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "co-resident phase failed: sidecar consent-WS did not "
                    "wire (#2417); cannot pin per-IP sharing",
                )
                await d.close()
                return

            # 1) Allow A -> learn a per-IP ACCEPT rule (forever = persistent
            #    in-memory TTL; survives until the revoke below).
            ca = _canonical(host_a)
            of_a = "/tmp/smoke_cr_a.out"
            _trigger(cont, host_a, of_a)
            rid_a = await d.wait_for(ca, 12.0)
            if rid_a is None:
                self._record_probe(
                    parent,
                    "allow A",
                    "no-request(!)",
                    None,
                    MISMATCH,
                    f"co-resident: A did NOT prompt ({host_a}); cannot learn "
                    f"the per-IP rule to pin #2352",
                )
                if not self.args.continue_run:
                    self._abort = True
                await d.close()
                return
            await d.verdict(rid_a, DECISION_ALLOWED, DURATION_FOREVER)
            await d.wait_resolution(rid_a, 15.0)
            await _wait_result(cont, of_a)  # A's own connection completes
            await asyncio.sleep(1.5)  # let the learned ACCEPT rule install

            # CANARY #1: B (same IP) connects with NO prompt + exit 0. Today
            # the rule is per-IP, so A's allow covers B. When #2352 lands
            # per-host allow, B will prompt -> this flips to MISMATCH.
            cb = _canonical(host_b)
            of_b1 = "/tmp/smoke_cr_b1.out"
            _trigger(cont, host_b, of_b1)
            intruder = await self._raw_wait_no_request(d, cb, window=6.0)
            text = await _wait_result(cont, of_b1, timeout=20.0)
            ec = _parse_exit(text)
            if intruder is None and ec == 0:
                sx, dx = (
                    PASS,
                    "co-resident: allow A leaked to B (shared per-IP rule, "
                    "#2352) -- CANARY: flips when per-host allow lands",
                )
            elif intruder is not None:
                sx, dx = (
                    MISMATCH,
                    "co-resident: allow A did NOT leak to B (B prompted) -- "
                    "CANARY FLIP: per-host allow may have landed (#2352); "
                    "update this pin",
                )
            else:
                sx, dx = (
                    FINDING,
                    f"co-resident: B did not connect cleanly (exit {ec}); "
                    f"not a #2352 signal (NFQUEUE/DNS)",
                )
            self._record_probe(parent, "allow A -> B", "shared", ec, sx, dx)
            if sx == MISMATCH and not self.args.continue_run:
                self._abort = True
                await d.close()
                return

            # 2) Revoke A -> today a NO-OP for this sequence. Canary #1's
            #    B connect re-resolved the shared IP, and _record_hosts
            #    keeps only the LATEST name per IP, so
            #    LEARNED[ip]["host"] is core-b by now;
            #    drop_for_host("core-a") matches nothing, the shared ACCEPT
            #    survives, and A itself also stays reachable -- the
            #    under-removal half of #2352.
            try:
                await d.revoke(rid_a)
            except Exception:
                pass
            await asyncio.sleep(2.0)  # let the server + sidecar drop the rule

            # CANARY #2: B connects with NO re-prompt (the revoke was a
            # no-op; the shared rule still covers it). When per-host (or
            # reverse-index) revoke lands, revoking A drops the shared rule
            # -> B re-prompts -> this flips. (Under a full #2352 fix CANARY
            # #1 already fired above, so this pin is the second half for the
            # per-host-REVOKE work specifically.)
            of_b2 = "/tmp/smoke_cr_b2.out"
            _trigger(cont, host_b, of_b2)
            rid_b2 = await d.wait_for(cb, 12.0)
            if rid_b2 is not None:
                # B re-prompted: what covered B is gone -- the canary flip.
                # Resolve the prompt so the run can continue cleanly.
                await d.verdict(rid_b2, DECISION_DENIED, DURATION_ONCE)
                await d.wait_resolution(rid_b2, 15.0)
                ec2 = _parse_exit(await _wait_result(cont, of_b2))
                sx2, dx2 = (
                    MISMATCH,
                    "co-resident: revoke A uncovered B (B re-prompted; "
                    "the shared rule was dropped) -- CANARY FLIP: per-host "
                    "or reverse-index revoke may have landed (#2352); "
                    "update this pin",
                )
            else:
                ec2 = _parse_exit(
                    await _wait_result(cont, of_b2, timeout=10.0)
                )
                if ec2 == 0:
                    sx2, dx2 = (
                        PASS,
                        "co-resident: revoke A did NOT uncover B (no "
                        "re-prompt; revoke no-op: B's resolve overwrote the "
                        "host record, #2352 under-removal) -- CANARY: flips "
                        "when per-host revoke lands",
                    )
                elif ec2 is None:
                    sx2, dx2 = (
                        FINDING,
                        "co-resident: post-revoke B hung (NFQUEUE/DNS; not a "
                        "#2352 signal)",
                    )
                else:
                    sx2, dx2 = (
                        FINDING,
                        f"co-resident: post-revoke B not clean (exit {ec2}, "
                        f"no re-prompt); not a #2352 signal (NFQUEUE/DNS)",
                    )
            self._record_probe(
                parent, "revoke A -> B", "no-reprompt", ec2, sx2, dx2
            )
            if sx2 == MISMATCH and not self.args.continue_run:
                self._abort = True
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                parent,
                "bring up co-resident ws",
                "error",
                None,
                MISMATCH,
                f"co-resident phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            if d is not None:
                await d.close()

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
            self._create_static_workspace, self.server, self.admin_auth
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
                self._delete_workspace, self.server, self.admin_auth, static_ws
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

    async def run_decider_scope_phase(self, pilot) -> None:
        """Workspace-scoped decider isolation + no-deploy-wide refusal
        (#2392, #2976).

        Cross-workspace isolation: a decider scoped to workspace A must NOT
        receive workspace B's ``egress_request`` (the registry's
        ``deciders_for`` filters by scope). Driven on the real stack with a
        second interactive workspace B.

        No deploy-wide flavor: a decider handshake with no ``workspace``
        param must be refused (#2976) -- an accepted one would be a
        cross-workspace consent path again.

        ``pilot`` is unused (signature symmetry); the textual app (decider
        #1, scoped to A = the isolation subject) is read directly via its
        ``controller.pending``.
        """
        if not self.args.decider_scope:
            return
        print(
            "\n--- decider scope: workspace-scoped isolation + no deploy-wide ---"
        )
        d_b: RawDecider | None = None
        cont_b: str | None = None
        step_tag = "create B"
        try:
            # 0) no deploy-wide flavor (#2976): the no-workspace handshake
            #    is refused pre-accept (uvicorn answers a bare HTTP 403).
            step_tag = "no-workspace handshake"
            await self._probe_no_workspace_decider_refused()

            ws_b = await self._new_workspace()
            self._extra_ws_ids.append(ws_b)
            print(f"workspace B {ws_b}  (interactive, for scope isolation)")
            # Keep B's container up + confirm readiness. A second workspace's
            # sidecar consent-WS has the same startup race as A's (#2417), so a
            # missing hold below is a finding, not a hard fail.
            step_tag = "B terminal WS"
            conn_b = await self._open_terminal_ws(ws_b)
            self._extra_ws_conns.append(
                (conn_b, asyncio.create_task(self._drain(conn_b)))
            )
            step_tag = "B container"
            cont_b = _container_for_workspace(ws_b)
            step_tag = "decider B"
            d_b = await self._connect_raw_decider(ws_b, ping=True)
            self._extra_deciders.append(d_b)
            await d_b.settle()

            # 1) isolation + B readiness: B's off-list SYN is held; d_b sees
            #    it, the A-scoped app does NOT. If d_b never sees it, B's
            #    sidecar WS wasn't wired yet (#2417) -> finding, not a hard fail.
            host_b = "openjdk.org"  # fresh: untouched elsewhere
            cb = _canonical(host_b)
            step_b = _Step(
                self.summary.total, host_b, "domain", False, "scope-B", "-"
            )
            of_b = f"/tmp/smoke_scope_b_{self.summary.total}.out"
            _trigger(cont_b, host_b, of_b)
            rid_b = await d_b.wait_for(cb, 20.0)
            if rid_b is None:
                self._record_probe(
                    step_b,
                    "B off-list -> d_b",
                    "no-request(!)",
                    None,
                    FINDING,
                    "B sidecar not ready (no hold); cannot assert isolation (#2417)",
                )
            else:
                leaked = await _wait_for_request(self.app, cb, 3.0)
                await d_b.verdict(rid_b, DECISION_DENIED, DURATION_ONCE)
                await d_b.wait_resolution(rid_b, 15.0)
                text_b = await _wait_result(cont_b, of_b)
                ec_b = _parse_exit(text_b)
                if leaked is not None:
                    sx, dx = (
                        MISMATCH,
                        "A-scoped decider saw B's request (isolation broken)",
                    )
                elif ec_b == EXIT_REFUSED:
                    sx, dx = PASS, ""
                elif ec_b == 0:
                    sx, dx = MISMATCH, "B deny let the connection through"
                else:
                    sx, dx = (
                        FINDING,
                        f"B conn not cleanly refused (exit {ec_b})",
                    )
                self._record_probe(
                    step_b,
                    "B off-list -> d_b only",
                    "isolated" if leaked is None else "leak(!)",
                    ec_b,
                    sx,
                    dx,
                )
                if sx == MISMATCH and not self.args.continue_run:
                    self._abort = True
                    return
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                _Step(
                    self.summary.total, "(scope)", "n/a", False, "scope", "-"
                ),
                "bring up B + deciders",
                "error",
                None,
                MISMATCH,
                f"could not set up the scope phase ({step_tag}): {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            # CRITICAL: a live d_b keeps workspace B interactive (and would
            # break later phases' premises), so close it eagerly; teardown is
            # a backstop (double-close is a safe no-op).
            if d_b is not None:
                await d_b.close()

    async def _probe_no_workspace_decider_refused(self) -> None:
        """#2976: a decider handshake with no ``workspace`` param must be
        refused pre-accept (uvicorn answers a bare HTTP 403). An accepted
        one would be a cross-workspace consent path -- anyone with an
        admin-ish grant deciding for every workspace.

        NB: a 403 here is also what an invalid/expired token yields -- the
        token is proven live by the app's own decider connect earlier in
        the run, so a 403 at this point is the workspace-param refusal.
        Deliberately probed with the ADMIN token (#2976): the admin is the
        strongest principal -- if even it cannot register without a
        workspace, no lesser identity can either."""
        step = _Step(
            self.summary.total, "(no-workspace)", "n/a", False, "scope-nw", "-"
        )
        try:
            ws_nw = await ws_connect(
                self.server,
                f"/ws/consent-decider?token={self.admin_auth['token']}",
                open_timeout=15,
            )
        except websockets.InvalidStatus as e:
            if e.response.status_code == 403:
                self._record_probe(
                    step, "no-workspace handshake", "refused", None, PASS, ""
                )
                return
            self._record_probe(
                step,
                "no-workspace handshake",
                f"http {e.response.status_code}",
                None,
                FINDING,
                "no-workspace handshake failed unexpectedly",
            )
            return
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                step,
                "no-workspace handshake",
                "error",
                None,
                FINDING,
                f"no-workspace handshake failed unexpectedly: {e!r}",
            )
            return
        # Accepted -- the cross-workspace path is back. Close it so it
        # cannot hold workspaces interactive for later phases.
        await ws_nw.close()
        self._record_probe(
            step,
            "no-workspace handshake",
            "accepted(!)",
            None,
            MISMATCH,
            "deploy-wide connect was accepted (cross-workspace consent "
            "path is back, #2976)",
        )
        if not self.args.continue_run:
            self._abort = True

    async def run_audit_distinction_phase(self, pilot) -> None:
        """Audit distinction: ``expired`` (timeout) vs ``denied`` (human) (#2392).

        A no-response consent hold auto-expires at the timeout and is resolved
        to the decider as ``egress_resolved{decision:"expired"}`` -- distinct
        from a human deny (``decision:"denied"``). The DB-level distinction
        (``decided_by`` NULL for a static-policy denial vs the decider's user id
        for a verdict; ``decision="expired"`` for a timeout) is model-internal
        with no HTTP read path and the issue forbids new server code, so this
        phase asserts the OBSERVABLE proxy: the ``egress_resolved`` frame's
        ``decision`` field. The static-denial half (off-list denied with no
        decider, no hold, no frame) is already covered by the no-decider phase.

        ``pilot`` is unused (signature symmetry); a fresh pinging RawDecider is
        the observer so it isn't reaped mid-phase.
        """
        if not self.args.audit_distinction:
            return
        print(
            "\n--- audit distinction: expired (timeout) vs denied (human) ---"
        )
        d: RawDecider | None = None
        try:
            d = await self._connect_raw_decider(self.ws_id, ping=True)
            self._extra_deciders.append(d)
            await d.settle()

            # 1) expired: off-list fresh host, NO verdict -> the consent
            #    timeout auto-expires the hold. The egress_resolved frame's
            #    decision must be "expired" (NOT "denied" -- that would
            #    mis-audit a timeout as a human deny).
            host_e = "ietf.org"  # fresh: untouched elsewhere
            ce = _canonical(host_e)
            step_e = _Step(
                self.summary.total,
                host_e,
                "domain",
                False,
                "audit-expired",
                "-",
            )
            of_e = f"/tmp/smoke_aud_e_{self.summary.total}.out"
            _trigger(self.container, host_e, of_e)
            rid_e = await d.wait_for(ce, 20.0)
            if rid_e is None:
                self._record_probe(
                    step_e,
                    "expired: hold",
                    "no-request(!)",
                    None,
                    FINDING,
                    "no hold surfaced (A sidecar readiness, #2417)",
                )
            else:
                decision = await d.wait_resolution(
                    rid_e, timeout=self.args.consent_timeout + 20
                )
                text_e = await _wait_result(self.container, of_e)
                ec_e = _parse_exit(text_e)
                if decision == "expired" and ec_e != 0:
                    sx, dx = PASS, ""
                elif decision == "expired":
                    sx, dx = MISMATCH, "expired but the connection succeeded"
                elif decision == "denied":
                    sx, dx = (
                        MISMATCH,
                        "timeout audited as 'denied' (not 'expired')",
                    )
                elif decision is None:
                    sx, dx = (
                        FINDING,
                        "no egress_resolved frame (sidecar hiccup)",
                    )
                else:
                    sx, dx = FINDING, f"unexpected resolution: {decision!r}"
                self._record_probe(
                    step_e,
                    "expired (no response)",
                    str(decision),
                    ec_e,
                    sx,
                    dx,
                )
                if sx == MISMATCH and not self.args.continue_run:
                    self._abort = True
                    return

            # 2) denied: off-list fresh host, a human deny verdict ->
            #    egress_resolved decision "denied" and the connection refused.
            host_d = "python.org"  # fresh
            cd = _canonical(host_d)
            step_d = _Step(
                self.summary.total,
                host_d,
                "domain",
                False,
                "audit-denied",
                "-",
            )
            of_d = f"/tmp/smoke_aud_d_{self.summary.total}.out"
            _trigger(self.container, host_d, of_d)
            rid_d = await d.wait_for(cd, 20.0)
            if rid_d is None:
                self._record_probe(
                    step_d,
                    "denied: hold",
                    "no-request(!)",
                    None,
                    FINDING,
                    "no hold surfaced (A sidecar readiness, #2417)",
                )
                return
            await d.verdict(rid_d, DECISION_DENIED, DURATION_ONCE)
            decision_d = await d.wait_resolution(rid_d, 15.0)
            text_d = await _wait_result(self.container, of_d)
            ec_d = _parse_exit(text_d)
            if decision_d == "denied" and ec_d == EXIT_REFUSED:
                sx, dx = PASS, ""
            elif decision_d == "denied":
                sx, dx = FINDING, f"denied but conn not refused (exit {ec_d})"
            else:
                sx, dx = (
                    MISMATCH,
                    f"human deny audited as {decision_d!r} (not 'denied')",
                )
            self._record_probe(
                step_d,
                "denied (human verdict)",
                str(decision_d),
                ec_d,
                sx,
                dx,
            )
            if sx == MISMATCH and not self.args.continue_run:
                self._abort = True
        except Exception as e:  # noqa: BLE001
            self._record_probe(
                _Step(
                    self.summary.total, "(audit)", "n/a", False, "audit", "-"
                ),
                "audit phase",
                "error",
                None,
                MISMATCH,
                f"audit phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            if d is not None:
                await d.close()

    async def _wait_allow_sidecar_wired(self, container: str) -> bool:
        """Allow-mode readiness analogue of :meth:`_wait_sidecar_ready`.

        The interactive probe keys on a consent request surfacing in the
        decider; allow mode surfaces none (the coordinator short-circuits to
        record+allow, #2406), so the signal that the sidecar consent-WS ->
        coordinator -> allow path is wired is a successful CONNECT. Retries
        fresh off-list hosts (cycling ``_SIDECAR_PROBE_HOSTS``) so a fail-close
        forged RST can't pin a ~10s REJECT backstop on the probe IP and mask
        wiring on the next attempt (#2417).
        """
        deadline = time.time() + _SIDECAR_READY_BUDGET
        attempt = 0
        while time.time() < deadline:
            host = _SIDECAR_PROBE_HOSTS[attempt % len(_SIDECAR_PROBE_HOSTS)]
            of = f"/tmp/smoke_allow_ready_{attempt}.out"
            _trigger(container, host, of)
            # A wired sidecar holds the SYN briefly, then the coordinator
            # auto-allows -> exit 0; an unwired one fail-closes (forged RST)
            # -> exit 7 at once. 12s covers the brief hold either way.
            text = await _wait_result(container, of, timeout=12.0)
            if _parse_exit(text) == 0:
                return True
            attempt += 1
            await asyncio.sleep(0.5)
        return False

    async def run_allow_phase(self, pilot) -> None:
        """#2411 ``allow`` egress mode (default-permit):

        * off-list egress CONNECTS -- the consent coordinator short-circuits to
          record+allow with no hold and no prompt (#2406), the opposite of the
          no-decider static phase (which denies+records off-list);
        * ``rejected_domains`` is STILL enforced (NXDOMAIN'd at the sidecar DNS
          layer, upstream of consent); and
        * a workspace-scoped consent decider is REFUSED (4003, same
          non-interactive gate as static, #2395/#2406).

        Uses its own allow-mode workspace + direct curl probes -- no decider
        attaches (none can). ``pilot`` is unused (signature symmetry).
        """
        if not self.args.allow_phase:
            return
        print(
            "\n--- #2411 allow mode: off-list connects, rejected denied, "
            "decider refused ---"
        )
        parent = _Step(
            self.summary.total, "(allow ws)", "n/a", False, "allow-mode", "-"
        )
        ws_id: str | None = None
        conn = None
        drain = None
        try:
            ws_id = await self._new_workspace(
                [],  # no allow-list: allow mode permits all non-rejected hosts
                ["evil.example.com"],  # rejected -> sidecar NXDOMAIN
                f"smoke-allow-{int(time.time() * 1000) % 100000}",
                "allow",
            )
            self._extra_ws_ids.append(ws_id)
            print(f"allow-mode workspace {ws_id}")
            conn = await self._open_terminal_ws(ws_id)
            drain = asyncio.create_task(self._drain(conn))
            self._extra_ws_conns.append((conn, drain))
            container = _container_for_workspace(ws_id)

            # 1) Wait for the sidecar consent-WS to wire before the scored
            #    probes -- an unwired sidecar fail-closes off-list to a forged
            #    RST and would false-positive as an allow-mode refusal.
            if not await self._wait_allow_sidecar_wired(container):
                self._record_probe(
                    parent,
                    "allow sidecar ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "allow-mode sidecar consent-WS did not wire (#2417); "
                    "cannot assert off-list connects",
                )
                return

            # 2) Off-list (default-permit): a fresh off-list host must CONNECT.
            host_off = "cloudflare.com"
            of_off = f"/tmp/smoke_allow_off_{self.summary.total}.out"
            _trigger(container, host_off, of_off)
            text_off = await _wait_result(container, of_off, timeout=30.0)
            ec_off = _parse_exit(text_off)
            if ec_off == 0:
                self._record_probe(
                    parent,
                    "allow off-list",
                    "connected",
                    ec_off,
                    PASS,
                    f"off-list {host_off} connected (default-permit)",
                )
            elif ec_off is None:
                self._record_probe(
                    parent,
                    "allow off-list",
                    "hung(!)",
                    None,
                    MISMATCH,
                    "allow-mode off-list hung (NFQUEUE/DNS)",
                )
            else:
                self._record_probe(
                    parent,
                    "allow off-list",
                    "refused(!)",
                    ec_off,
                    MISMATCH,
                    f"allow-mode off-list refused (exit {ec_off}); must connect",
                )
            if self._abort:
                return

            # 3) rejected_domains enforced: the rejected host is NXDOMAIN'd at
            #    the sidecar DNS layer even in allow mode.
            host_rej = "evil.example.com"
            of_rej = f"/tmp/smoke_allow_rej_{self.summary.total}.out"
            _trigger(container, host_rej, of_rej)
            text_rej = await _wait_result(container, of_rej, timeout=20.0)
            ec_rej = _parse_exit(text_rej)
            if ec_rej == 0:
                self._record_probe(
                    parent,
                    "allow rejected",
                    "leak(!)",
                    ec_rej,
                    MISMATCH,
                    "rejected host succeeded in allow mode (leak)",
                )
            else:
                self._record_probe(
                    parent,
                    "allow rejected",
                    "denied",
                    ec_rej,
                    PASS,
                    f"rejected host denied in allow mode (exit {ec_rej})",
                )
            if self._abort:
                return

            # 4) A workspace-scoped consent decider must be REFUSED (4003),
            #    mirroring run_static_refusal_phase (#2395/#2406).
            url = (
                f"/ws/consent-decider?token={self.auth['token']}"
                f"&workspace={ws_id}"
            )
            try:
                ws = await ws_connect(self.server, url, open_timeout=15)
            except Exception as e:  # noqa: BLE001  (refused at the handshake)
                self._record_probe(
                    parent,
                    "decider->allow",
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
                    # against an allow workspace (#2406 violation).
                    self._record_probe(
                        parent,
                        "decider->allow",
                        "attached(!)",
                        None,
                        MISMATCH,
                        "a decider registered against an allow workspace",
                    )
                except Exception as e:  # noqa: BLE001  (ConnectionClosed / timeout)
                    if "Closed" in type(e).__name__:
                        code = getattr(e, "code", None)
                        self._record_probe(
                            parent,
                            "decider->allow",
                            f"refused({code})",
                            None,
                            PASS,
                            "registration refused (connection closed)",
                        )
                    else:
                        self._record_probe(
                            parent,
                            "decider->allow",
                            "no-close",
                            None,
                            FINDING,
                            f"allow decider: no frame/close: {e!r}",
                        )
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001  (phase bring-up failure)
            self._record_probe(
                parent,
                "allow phase",
                "error",
                None,
                MISMATCH,
                f"allow-mode phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True

    async def run_restart_phase(self, pilot) -> None:
        """#2364/#2346 verdict semantics across a workspace container restart.

        * a ``forever`` **allow** survives -- it mutates ``allowed_domains``
          (#2368), which the sidecar re-reads on (re)start, so the host
          reconnects with no re-prompt;
        * a ``forever`` **deny** survives -- it mutates ``rejected_domains``
          (#2369), so the host is re-denied with no re-prompt; and
        * a ``tilrestart`` verdict is **cleared** (#2346/#2349) -- the sidecar's
          in-memory rule dies with the container and the DB row is reaped on
          (re)start, so the host re-prompts.

        Uses its own interactive workspace + RawDecider (independent of the
        hero workspace's textual decider). Grants the three verdicts, issues ONE
        container restart (POST /restart), re-opens the terminal WS + reattaches
        a fresh decider, re-wires the sidecar, and asserts the post-restart
        behavior. ``pilot`` is unused (signature symmetry).
        """
        if not self.args.restart_phase:
            return
        print(
            "\n--- #2364/#2346 restart: forever survives, tilrestart cleared ---"
        )
        parent = _Step(
            self.summary.total,
            "(restart ws)",
            "n/a",
            False,
            "restart-semantics",
            "-",
        )
        # Distinct fresh hosts (not in the probe pool or any default list) so
        # the three verdicts don't collide and don't perturb the readiness probe.
        host_a, host_b, host_c = (
            "cloudflare.com",
            "github.com",
            "wikipedia.org",
        )
        canon_a, canon_b, canon_c = (
            _canonical(host_a),
            _canonical(host_b),
            _canonical(host_c),
        )
        rws: str | None = None
        conn = None
        d: RawDecider | None = None
        try:
            rws = await self._new_workspace(
                [],  # no allow-list: every probe host prompts
                [],  # no rejected-list
                f"smoke-restart-{int(time.time() * 1000) % 100000}",
            )
            self._extra_ws_ids.append(rws)
            print(f"restart-semantics workspace {rws}")
            conn = await self._open_terminal_ws(rws)
            asyncio.create_task(self._drain(conn))
            container = _container_for_workspace(rws)
            d = await self._connect_raw_decider(rws, ping=True)
            await d.settle()

            # The sidecar consent-WS must be wired before verdicts grant cleanly.
            if not await self._probe_sidecar_ready_raw(
                d, container, _SIDECAR_PROBE_HOSTS[0]
            ):
                self._record_probe(
                    parent,
                    "restart sidecar ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "restart-phase sidecar not wired; cannot assert durability",
                )
                return

            async def _grant(
                host: str, canon: str, decision: str, duration: str, label: str
            ) -> str | None:
                # Trigger + wait for the request + apply the verdict. Returns the
                # request id, or None (recorded MISMATCH) if no request surfaced.
                _trigger(
                    container, host, f"/tmp/smoke_restart_grant_{label}.out"
                )
                rid = await d.wait_for(canon, timeout=15.0)
                if rid is None:
                    self._record_probe(
                        parent,
                        f"{label} grant",
                        "no-request(!)",
                        None,
                        MISMATCH,
                        f"restart-phase no request for {host} ({label} grant)",
                    )
                    return None
                await d.verdict(rid, decision, duration)
                await d.wait_resolution(rid, timeout=20.0)
                return rid

            # 1) Grant forever-allow (A), forever-deny (B), tilrestart-allow (C).
            if (
                await _grant(
                    host_a,
                    canon_a,
                    DECISION_ALLOWED,
                    DURATION_FOREVER,
                    "forever-allow",
                )
                is None
            ):
                return
            if (
                await _grant(
                    host_b,
                    canon_b,
                    DECISION_DENIED,
                    DURATION_FOREVER,
                    "forever-deny",
                )
                is None
            ):
                return
            if (
                await _grant(
                    host_c,
                    canon_c,
                    DECISION_ALLOWED,
                    DURATION_TILRESTART,
                    "tilrestart",
                )
                is None
            ):
                return

            # Sanity: tilrestart was in effect pre-restart (the sidecar's
            # in-memory ACCEPT lets C connect). Best-effort -- CDN/per-IP
            # rotation can re-prompt a freshly-learned IP (#2399) -> finding.
            of_c = f"/tmp/smoke_restart_cpre_{self.summary.total}.out"
            _trigger(container, host_c, of_c)
            ec_c = _parse_exit(
                await _wait_result(container, of_c, timeout=20.0)
            )
            if ec_c == 0:
                self._record_probe(
                    parent,
                    "tilrestart pre-restart",
                    "connected",
                    ec_c,
                    PASS,
                    "tilrestart allow in effect before restart",
                )
            else:
                self._record_probe(
                    parent,
                    "tilrestart pre-restart",
                    "no-connect",
                    ec_c,
                    FINDING,
                    "tilrestart not in effect pre-restart (CDN/per-IP rotation?)",
                )
            if self._abort:
                return

            # 2) ONE container restart. The old terminal WS dies with the
            #    container; reopen it + reattach a fresh decider, then re-wire
            #    the sidecar before the scored post-restart probes.
            print("  restarting workspace container ...")
            await asyncio.to_thread(
                self._restart_workspace, self.server, self.auth, rws
            )
            try:
                await conn.close()
            except Exception:
                pass
            conn = await self._open_terminal_ws(rws)
            asyncio.create_task(self._drain(conn))
            await d.close()
            d = await self._connect_raw_decider(rws, ping=True)
            await d.settle()
            if not await self._probe_sidecar_ready_raw(
                d, container, _SIDECAR_PROBE_HOSTS[1]
            ):
                self._record_probe(
                    parent,
                    "restart sidecar re-ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "restart-phase sidecar not wired after restart; cannot assert",
                )
                return

            # 3) forever-allow survives -> A connects (allowed_domains persisted).
            of_a = f"/tmp/smoke_restart_a_{self.summary.total}.out"
            _trigger(container, host_a, of_a)
            ec_a = _parse_exit(
                await _wait_result(container, of_a, timeout=25.0)
            )
            if ec_a == 0:
                self._record_probe(
                    parent,
                    "forever-allow post-restart",
                    "connected",
                    ec_a,
                    PASS,
                    "forever allow survived restart (allowed_domains, #2368)",
                )
            else:
                self._record_probe(
                    parent,
                    "forever-allow post-restart",
                    "no-connect(!)",
                    ec_a,
                    MISMATCH,
                    "forever-allow did not survive restart "
                    "(allowed_domains not re-read?)",
                )
            if self._abort:
                return

            # 4) forever-deny survives -> B denied (rejected_domains persisted).
            of_b = f"/tmp/smoke_restart_b_{self.summary.total}.out"
            _trigger(container, host_b, of_b)
            ec_b = _parse_exit(
                await _wait_result(container, of_b, timeout=25.0)
            )
            if ec_b is not None and ec_b != 0:
                self._record_probe(
                    parent,
                    "forever-deny post-restart",
                    "denied",
                    ec_b,
                    PASS,
                    "forever deny survived restart (rejected_domains, #2369)",
                )
            elif ec_b == 0:
                self._record_probe(
                    parent,
                    "forever-deny post-restart",
                    "leak(!)",
                    ec_b,
                    MISMATCH,
                    "forever-deny leaked after restart (rejected_domains dropped?)",
                )
            else:
                self._record_probe(
                    parent,
                    "forever-deny post-restart",
                    "prompted(!)",
                    None,
                    MISMATCH,
                    "forever-deny did not survive restart (re-prompted, not persisted)",
                )
            if self._abort:
                return

            # 5) tilrestart cleared -> C re-prompts (in-memory rule died + DB row
            #    reaped). If it connected with no prompt instead, it survived.
            of_c2 = f"/tmp/smoke_restart_cpost_{self.summary.total}.out"
            _trigger(container, host_c, of_c2)
            rid_c = await d.wait_for(canon_c, timeout=15.0)
            if rid_c is not None:
                # Release the held SYN so curl doesn't linger.
                await d.verdict(rid_c, DECISION_ALLOWED, DURATION_ONCE)
                await d.wait_resolution(rid_c, timeout=15.0)
                self._record_probe(
                    parent,
                    "tilrestart post-restart",
                    "re-prompted",
                    None,
                    PASS,
                    "tilrestart cleared on restart (reaped, #2346/#2349)",
                )
            else:
                ec_c2 = _parse_exit(_read_outfile(container, of_c2))
                self._record_probe(
                    parent,
                    "tilrestart post-restart",
                    "no-prompt(!)",
                    ec_c2,
                    MISMATCH,
                    "tilrestart survived restart (not reaped; rule/row kept)",
                )
        except Exception as e:  # noqa: BLE001  (phase bring-up failure)
            self._record_probe(
                parent,
                "restart phase",
                "error",
                None,
                MISMATCH,
                f"restart-phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            if d is not None:
                try:
                    await d.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass

    async def run_pause_phase(self, pilot) -> None:
        """#2332/#2389: pausing consent filtering silences the interactive hold
        workspace-wide for a window, while static policy keeps applying.

        * While paused, an off-list host CONNECTS (exit 0) with NO prompt
          surfaced to a decider (the hold auto-allows).
        * After resume, the SAME off-list host is HELD again and surfaces a
          request.
        * ``allowed_domains`` / ``rejected_domains`` enforcement is unchanged
          by pause -- those are enforced at the sidecar DNS layer (ACCEPT /
          NXDOMAIN) upstream of the consent gate, so a paused workspace can't
          let a rejected host through or be affected by an allow-list entry.

        Uses its own interactive workspace + RawDecider (workspace-level
        state, like the restart phase; independent of the hero textual
        decider). ``pilot`` is unused (signature symmetry).
        """
        if not self.args.pause_phase:
            return
        print(
            "\n--- #2332 pause consent filtering: hold silenced, "
            "policy kept ---"
        )
        parent = _Step(
            self.summary.total,
            "(pause ws)",
            "n/a",
            False,
            "pause",
            "-",
        )
        # Fresh, distinct hosts (not in the probe pool or any default list):
        # off-list must connect under pause; allow-list + rejected exercise
        # the static policy that pause must leave untouched.
        host_off = "ubuntu.com"
        host_allow = "example.com"
        host_rej = "kernel.org"
        canon_off = _canonical(host_off)
        pws: str | None = None
        conn = None
        d: RawDecider | None = None
        try:
            pws = await self._new_workspace(
                [host_allow],  # allow-list: DNS-layer allow, pause-independent
                [host_rej],  # rejected: DNS-layer NXDOMAIN, pause-independent
                f"smoke-pause-{int(time.time() * 1000) % 100000}",
            )
            self._extra_ws_ids.append(pws)
            print(f"pause workspace {pws}")
            conn = await self._open_terminal_ws(pws)
            asyncio.create_task(self._drain(conn))
            container = _container_for_workspace(pws)
            d = await self._connect_raw_decider(pws, ping=True)
            await d.settle()

            # The sidecar consent-WS must be wired before pause/hold behavior
            # is asserted; an unwired sidecar fail-closes off-list and would
            # false-positive as a pause refusal (#2417).
            if not await self._probe_sidecar_ready_raw(
                d, container, _SIDECAR_PROBE_HOSTS[2]
            ):
                self._record_probe(
                    parent,
                    "pause sidecar ready",
                    "no-request(!)",
                    None,
                    FINDING,
                    "pause-phase sidecar not wired; cannot assert pause behavior",
                )
                return

            # 1) PAUSE. The decider is workspace-scoped (the hero owner), so
            #    share-terminals is satisfied; a pause_ack ok=True must return.
            if not await d.pause("15m"):
                self._record_probe(
                    parent,
                    "pause",
                    "nack(!)",
                    None,
                    MISMATCH,
                    "pause_ack was not ok (authz/protocol failed)",
                )
                return
            self._record_probe(
                parent,
                "pause",
                "paused",
                None,
                PASS,
                "consent filtering paused workspace-wide (15m window)",
            )

            # 2) While paused: off-list CONNECTS with NO prompt (auto-allow).
            of_off = f"/tmp/smoke_pause_off_{self.summary.total}.out"
            _trigger(container, host_off, of_off)
            intruder = await d.wait_no_request(canon_off, window=6.0)
            if intruder is not None:
                # The hold was not silenced -- release the SYN, then flag it.
                await d.verdict(intruder, DECISION_ALLOWED, DURATION_ONCE)
                await d.wait_resolution(intruder, timeout=15.0)
                self._record_probe(
                    parent,
                    "paused off-list",
                    "request(!)",
                    None,
                    MISMATCH,
                    "off-list prompted while paused (hold not silenced)",
                )
            else:
                text_off = await _wait_result(container, of_off, timeout=25.0)
                ec_off = _parse_exit(text_off)
                if ec_off == 0:
                    self._record_probe(
                        parent,
                        "paused off-list",
                        "connected",
                        ec_off,
                        PASS,
                        "off-list auto-allowed while paused (no prompt)",
                    )
                elif ec_off is None:
                    self._record_probe(
                        parent,
                        "paused off-list",
                        "hung(!)",
                        None,
                        MISMATCH,
                        "off-list hung while paused (NFQUEUE/DNS or no auto-allow)",
                    )
                else:
                    self._record_probe(
                        parent,
                        "paused off-list",
                        "refused(!)",
                        ec_off,
                        MISMATCH,
                        f"off-list refused while paused (exit {ec_off}); "
                        "must auto-allow",
                    )
            if self._abort:
                return

            # 3) While paused: allow-list host STILL connects (DNS-layer allow,
            #    unaffected by pause). A prompt or a refusal means the pause
            #    perturbed the static policy.
            of_al = f"/tmp/smoke_pause_al_{self.summary.total}.out"
            _trigger(container, host_allow, of_al)
            intruder_al = await d.wait_no_request(
                _canonical(host_allow), window=5.0
            )
            text_al = await _wait_result(container, of_al, timeout=20.0)
            ec_al = _parse_exit(text_al)
            if intruder_al is not None:
                await d.verdict(intruder_al, DECISION_ALLOWED, DURATION_ONCE)
                await d.wait_resolution(intruder_al, timeout=15.0)
                self._record_probe(
                    parent,
                    "paused allow-list",
                    "request(!)",
                    ec_al,
                    MISMATCH,
                    "allow-list refused while paused (prompted; DNS-layer "
                    "allow bypassed?)",
                )
            elif ec_al == 0:
                self._record_probe(
                    parent,
                    "paused allow-list",
                    "connected",
                    ec_al,
                    PASS,
                    "allow-list host connected while paused (unchanged)",
                )
            else:
                self._record_probe(
                    parent,
                    "paused allow-list",
                    "refused(!)",
                    ec_al,
                    MISMATCH,
                    f"allow-list refused while paused (exit {ec_al}); "
                    "static allow unchanged by pause",
                )
            if self._abort:
                return

            # 4) While paused: rejected host STILL denied (DNS-layer NXDOMAIN,
            #    unaffected by pause). A success is a leak; a prompt means the
            #    pause routed a rejected host into the consent gate.
            of_rj = f"/tmp/smoke_pause_rj_{self.summary.total}.out"
            _trigger(container, host_rej, of_rj)
            intruder_rj = await d.wait_no_request(
                _canonical(host_rej), window=5.0
            )
            text_rj = await _wait_result(container, of_rj, timeout=20.0)
            ec_rj = _parse_exit(text_rj)
            if intruder_rj is not None:
                await d.verdict(intruder_rj, DECISION_DENIED, DURATION_ONCE)
                await d.wait_resolution(intruder_rj, timeout=15.0)
                self._record_probe(
                    parent,
                    "paused rejected",
                    "request(!)",
                    ec_rj,
                    MISMATCH,
                    "rejected host prompted while paused (DNS-layer deny bypassed?)",
                )
            elif ec_rj == 0:
                self._record_probe(
                    parent,
                    "paused rejected",
                    "leak(!)",
                    ec_rj,
                    MISMATCH,
                    "rejected host leaked while paused (DNS-layer deny broken)",
                )
            else:
                self._record_probe(
                    parent,
                    "paused rejected",
                    "denied",
                    ec_rj,
                    PASS,
                    f"rejected host denied while paused (exit {ec_rj}; "
                    "unchanged)",
                )
            if self._abort:
                return

            # 5) RESUME. Clearing the pause must ack ok=True.
            if not await d.unpause():
                self._record_probe(
                    parent,
                    "resume",
                    "nack(!)",
                    None,
                    MISMATCH,
                    "unpause_ack was not ok (resume failed)",
                )
                return
            self._record_probe(
                parent,
                "resume",
                "resumed",
                None,
                PASS,
                "consent filtering resumed (pause cleared)",
            )

            # 6) After resume: the SAME off-list host is HELD again and surfaces
            #    a request (the paused allow was once -> nothing lingered). A
            #    silent connect means the pause lingered; a prompt is the PASS.
            of_off2 = f"/tmp/smoke_pause_off2_{self.summary.total}.out"
            _trigger(container, host_off, of_off2)
            rid_off = await d.wait_for(canon_off, timeout=15.0)
            if rid_off is not None:
                await d.verdict(rid_off, DECISION_ALLOWED, DURATION_ONCE)
                await d.wait_resolution(rid_off, timeout=15.0)
                ec_off2 = _parse_exit(
                    await _wait_result(container, of_off2, timeout=15.0)
                )
                self._record_probe(
                    parent,
                    "resumed off-list",
                    "re-held",
                    ec_off2,
                    PASS,
                    "off-list re-held for consent after resume",
                )
            else:
                ec_off2 = _parse_exit(
                    await _wait_result(container, of_off2, timeout=12.0)
                )
                self._record_probe(
                    parent,
                    "resumed off-list",
                    "no-request(!)",
                    ec_off2,
                    MISMATCH,
                    f"off-list not re-held after resume (exit {ec_off2}; "
                    "pause lingered?)",
                )
        except Exception as e:  # noqa: BLE001  (phase bring-up failure)
            self._record_probe(
                parent,
                "pause phase",
                "error",
                None,
                MISMATCH,
                f"pause-phase failed: {e!r}",
            )
            if not self.args.continue_run:
                self._abort = True
        finally:
            if d is not None:
                try:
                    await d.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass

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
                await self._wait_sidecar_ready(pilot)
                for step in plan:
                    try:
                        res = await self._run_step(pilot, step)
                    except Exception as exc:  # noqa: BLE001
                        if _is_container_gone_error(exc) and self.container:
                            # The workspace container vanished mid-run (a
                            # `podman exec` returned 125 'no such container').
                            # Capture why NOW -- before teardown removes the
                            # rest -- then abort: every later step would just
                            # re-fail the same exec, so record one FINDING
                            # (CONTAINER-GONE) instead of a spurious cascade of
                            # UNEXPECTED-ERROR mismatches (#2475).
                            diag = self._capture_container_diagnostics(
                                self.container, step.idx + 1
                            )
                            res = _Result(
                                step,
                                _canonical(step.host),
                                True,
                                "",
                                "?",
                                "container-gone",
                                None,
                                FINDING,
                                detail=(
                                    f"container gone at step {step.idx + 1}: "
                                    f"podman exec rc="
                                    f"{getattr(exc, 'returncode', '?')} "
                                    f"({exc.__class__.__name__}); diagnostics "
                                    f"-> {diag or '(capture failed)'}"
                                ),
                            )
                            res.outcome = _outcome_name(res)
                            self.summary.total += 1
                            self.summary.findings += 1
                            self.summary.rows.append(res)
                            self._print_row(res)
                            self._abort = True
                            stop = True
                            break
                        res = _Result(
                            step,
                            _canonical(step.host),
                            True,
                            "",
                            "?",
                            "error",
                            None,
                            MISMATCH,
                            detail=f"step raised: {exc!r}",
                        )
                        res.outcome = _outcome_name(res)
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
                    await self.run_mixed_users_phase(pilot)
                if not stop and not self._abort:
                    await self.run_snapshot_replay_phase(pilot)
                if not stop and not self._abort:
                    await self.run_fanout_phase(pilot)
                if not stop and not self._abort:
                    await self.run_revoke_phase(pilot)
                if not stop and not self._abort:
                    await self.run_rejected_phase(pilot)
                if not stop and not self._abort:
                    await self.run_fail_closed_phase(pilot)
                if not stop and not self._abort:
                    await self.run_decider_scope_phase(pilot)
                if not stop and not self._abort:
                    await self.run_audit_distinction_phase(pilot)
                if not stop and not self._abort:
                    await self.run_allow_phase(pilot)
                if not stop and not self._abort:
                    await self.run_restart_phase(pilot)
                # Consent-filtering pause/resume (#2332/#2389): its own
                # interactive workspace + RawDecider (workspace-level state,
                # like the restart phase), so it runs alongside it.
                if not stop and not self._abort:
                    await self.run_pause_phase(pilot)
                # Controlled-DNS phases (#2424): host-scope + port-scope use their
                # own workspace + raw decider (independent of the textual app),
                # so they run last inside run_test.
                if not stop and not self._abort:
                    await self.run_host_scope_phase(pilot)
                if not stop and not self._abort:
                    await self.run_port_scope_phase(pilot)
                if not stop and not self._abort:
                    await self.run_coresident_phase(pilot)
            # run_test exited -> the decider WS dropped -> the server deregistered
            # the decider -> the workspace reverted to static allow-list (#2308).
            # _shared_d2 (the multi-decider / snapshot raw decider) is a SEPARATE
            # WS that outlives run_test (closed only in teardown) and never
            # pings, so close it explicitly here -- otherwise it can still be
            # live (within the 45s reap window) during the static phase,
            # keeping the workspace interactive and producing a false "held"
            # mismatch (#2413).
            if not stop and not self._abort and self.args.static_phase:
                if self._shared_d2 is not None:
                    await self._shared_d2.close()
                    self._shared_d2 = None
                await self.run_no_decider_phase(pilot)
        except Exception as exc:
            # An uncaught exception escaped a phase (e.g. a podman exec against
            # a workspace container that disappeared mid-run). Record it so the
            # summary reflects the abort instead of dumping a raw asyncio
            # traceback on exit (#2456). KeyboardInterrupt / CancelledError is
            # deliberately NOT caught here (they are BaseException, not
            # Exception) -- Ctrl-C propagates to main()'s clean exit-130.
            if _is_container_gone_error(exc) and self.container:
                # A phase's podman exec hit a gone container: capture the death
                # diagnostics (who removed it -- only klangkd removes workspace
                # containers) and record a CONTAINER-GONE finding rather than a
                # generic UNEXPECTED-ERROR (#2475).
                diag = self._capture_container_diagnostics(
                    self.container, self.summary.total
                )
                self._record_probe(
                    _Step(
                        self.summary.total,
                        "(run)",
                        "n/a",
                        False,
                        "run",
                        "-",
                    ),
                    "container gone",
                    "container-gone",
                    None,
                    FINDING,
                    f"run aborted: workspace container gone mid-run; "
                    f"diagnostics -> {diag or '(capture failed)'} (#2475)",
                )
            else:
                self._record_probe(
                    _Step(
                        self.summary.total, "(run)", "n/a", False, "run", "-"
                    ),
                    "uncaught exception",
                    "error",
                    None,
                    MISMATCH,
                    f"run aborted: uncaught {exc!r}",
                )
            stop = True
        finally:
            self._capture_sidecar_logs()
            await self.teardown()
        self._print_summary(stop)
        return 1 if self.summary.mismatches else 0

    async def teardown(self) -> None:
        if self.dns is not None:
            try:
                self.dns.stop()
            except Exception:
                pass
            self.dns = None
        if self._containers_conf is not None:
            import shutil

            os.environ.pop("CONTAINERS_CONF", None)
            try:
                shutil.rmtree(
                    os.path.dirname(self._containers_conf), ignore_errors=True
                )
            except Exception:
                pass
            self._containers_conf = None
        if self._shared_d2 is not None:
            await self._shared_d2.close()
            self._shared_d2 = None
        for d in self._extra_deciders:
            try:
                await d.close()
            except Exception:
                pass
        self._extra_deciders.clear()
        for conn, drain in self._extra_ws_conns:
            drain.cancel()
            try:
                await conn.close()
            except Exception:
                pass
        self._extra_ws_conns.clear()
        if self.server and self.auth and self.args.delete_workspace:
            for wid in self._extra_ws_ids:
                try:
                    await asyncio.to_thread(
                        self._delete_workspace,
                        self.server,
                        self.admin_auth,
                        wid,
                    )
                except Exception:
                    pass
        self._extra_ws_ids.clear()
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
                    self._delete_workspace,
                    self.server,
                    self.admin_auth,
                    self.ws_id,
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

    @staticmethod
    def _restart_workspace(server, auth, ws_id) -> None:
        # POST /api/v1/workspaces/{id}/restart: stop+remove the running container
        # and start a fresh one with the same config (#1244). The fresh start
        # re-runs start_container -> clear_tilrestart_duration (reaps
        # tilrestart verdicts) and re-reads allowed/rejected_domains (forever
        # verdicts persist). Blocks until the new container is up; the sidecar
        # consent-WS reconnects on its own backoff afterwards.
        client = httpx.Client(
            base_url=server["url"], headers=auth["headers"], timeout=120
        )
        try:
            r = client.post(f"/api/v1/workspaces/{ws_id}/restart")
            r.raise_for_status()
        finally:
            client.close()

    @staticmethod
    def _print_outcome_tally(rows: list[_Result]) -> None:
        counts: dict[str, int] = {}
        for r in rows:
            if r.outcome:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
        if not counts:
            return
        print("\noutcome names:")
        for name in sorted(counts):
            print(f"  {name:<26} x{counts[name]}")

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
                    f"  [{r.step.idx + 1}]{' [' + r.outcome + ']' if r.outcome else ''} "
                    f"{r.step.host} ({r.step.kind}) "
                    f"action={r.action_taken} sidecar={r.sidecar} "
                    f"conn={_exit_label(r.exit_code)}  {r.detail}"
                )
        mism = [r for r in s.rows if r.status == MISMATCH]
        if mism:
            print("\nmismatches:")
            for r in mism:
                print(
                    f"  [{r.step.idx + 1}]{' [' + r.outcome + ']' if r.outcome else ''} "
                    f"{r.step.host} ({r.step.kind}) "
                    f"expect_req={r.expect_request} expect_conn={r.expect_conn} "
                    f"action={r.action_taken} sidecar={r.sidecar} "
                    f"conn={_exit_label(r.exit_code)}  {r.detail}"
                )
        self._print_outcome_tally(s.rows)
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
        "--no-mixed-users",
        dest="mixed_users",
        action="store_false",
        help="skip the mixed-users phase (#3112): two identities (the acting "
        "one plus the member, or the admin under --as-member) decide the "
        "same workspace at once -- shared visibility, first-decision-wins + "
        "carryover across users, decided_by attribution, cross-user revoke",
    )
    p.add_argument(
        "--snapshot",
        dest="snapshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="the reconnect snapshot-replay phase (DEFAULT ON under controlled "
        "DNS, #2424: with single-IP test hosts it is a hard PASS/FAIL; use "
        "--no-snapshot to skip. Without --controlled-dns the phase is skipped "
        "(indeterminate on real multi-IP hosts).",
    )
    p.add_argument(
        "--no-fanout",
        dest="fanout",
        action="store_false",
        help="skip the per-connection verdict-cache + N-way fan-out phase "
        "(#2361/#2366/#2337: N concurrent distinct hosts surface N distinct "
        "requests; a once on one connection does not satisfy another; a held "
        "connection's retransmits dedupe to one request).",
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
        "--no-decider-scope",
        dest="decider_scope",
        action="store_false",
        help="skip the workspace-scoped decider isolation phase (#2392/#2976)",
    )
    p.add_argument(
        "--no-audit-distinction",
        dest="audit_distinction",
        action="store_false",
        help="skip the expired-vs-denied audit-distinction phase (#2392)",
    )
    p.add_argument(
        "--no-allow-phase",
        dest="allow_phase",
        action="store_false",
        help="skip the #2411 allow-egress-mode phase (off-list connects, "
        "rejected denied, decider refused)",
    )
    p.add_argument(
        "--no-restart-phase",
        dest="restart_phase",
        action="store_false",
        help="skip the #2364/#2346 restart verdict-semantics phase "
        "(forever survives, tilrestart reaped)",
    )
    p.add_argument(
        "--no-pause-phase",
        dest="pause_phase",
        action="store_false",
        help="skip the #2332/#2389 consent-filtering pause/resume phase "
        "(off-list auto-allows while paused, re-holds after resume; "
        "allow-list/rejected policy unchanged)",
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
    p.add_argument(
        "--controlled-dns",
        dest="controlled_dns",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="bring up the controlled-DNS fixture (#2424) and point the real "
        "sidecar at it so chosen hostnames resolve to single stable test IPs "
        "(default ON). Disabling reverts the snapshot/host-scope/port-scope "
        "phases to their pre-#2424 behavior (skipped / indeterminate).",
    )
    p.add_argument(
        "--sidecar-image",
        default="localhost/klangk-network-sidecar:latest",
        help="the network-sidecar image the controlled-DNS fixture reuses for "
        "its DNS forwarder + multi-IP HTTP/HTTPS target (default: "
        "localhost/klangk-network-sidecar:latest).",
    )
    p.add_argument(
        "--no-host-scope",
        dest="host_scope",
        action="store_false",
        help="skip the host-scope (exact/inclusive/subdomains) phase (#2377/#2424)",
    )
    p.add_argument(
        "--no-port-scope",
        dest="port_scope",
        action="store_false",
        help="skip the port-scope (host:443 vs :80) phase (#2424)",
    )
    p.add_argument(
        "--no-coresident",
        dest="coresident",
        action="store_false",
        help="skip the co-resident-hosts phase (canary for per-IP allow/revoke "
        "sharing, #2352/#2440)",
    )
    p.add_argument(
        "--as-member",
        dest="as_member",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="run the consent flow as a plain workspace member instead of "
        "the seeded admin (#2976): the admin only bootstraps (create the "
        "member, the workspaces, and share them to its coders role), then "
        "every decider connection, verdict, and terminal WS runs on the "
        "member's token -- consent authority flows from the workspace ACL "
        "alone, with no instance-admin grants anywhere in the run. The "
        "no-workspace decider probe still uses the admin token (the "
        "strongest principal).",
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="don't run the smoketest; reclaim leaked containers from prior "
        "interrupted runs (ctrl-dns-* fixtures + klangk-net-* sidecars whose "
        "owning klangkd is dead) then exit (#2443)",
    )
    args = p.parse_args()
    if args.cleanup:
        return _run_cleanup()
    if args.seed is None:
        args.seed = random.randrange(1 << 30)
    if args.consent_timeout < 4:
        p.error("--consent-timeout must be >= 4")
    inst = SmokeTest(args)

    def _on_term(signum, frame):
        # SIGTERM (``kill <pid>``, agent timeouts) terminates without running
        # the async ``finally`` or ``atexit`` — which is how the unlabelled
        # ctrl-dns fixtures leak (#2443). Run the synchronous container
        # teardown here, then exit with the conventional 128+signal code.
        inst._cleanup_sync()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_term)
    try:
        return asyncio.run(inst.run())
    except KeyboardInterrupt:
        # Ctrl-C: asyncio.run cancels the main task (run()'s async ``finally``
        # still runs teardown during the cancellation) then re-raises
        # KeyboardInterrupt. Catch it for a clean exit -- the conventional
        # 128+SIGINT (130) and no traceback (#2456). The ``finally`` below
        # (_cleanup_sync) is the backstop if a second Ctrl-C interrupts
        # teardown itself.
        print("\ninterrupted", file=sys.stderr)
        return 128 + signal.SIGINT
    except Exception as exc:
        # Backstop for an exception that escaped run()'s own handling (e.g.
        # setup() or teardown() raised). Print a concise error -- not the full
        # asyncio-internals traceback -- and exit non-zero (#2456).
        print(f"\nsmoketest aborted: {exc!r}", file=sys.stderr)
        return 1
    finally:
        # Normal exit / uncaught exception / KeyboardInterrupt: belt-and-
        # suspenders in case the async ``finally`` was itself interrupted
        # (e.g. a second Ctrl-C). Idempotent — no-ops if already cleaned.
        inst._cleanup_sync()


def _run_cleanup() -> int:
    """Reclaim leaked smoketest containers without a full run (#2443).

    Removes:

      * every ``ctrl-dns-*`` fixture (unlabelled, so invisible to klangkd's
        reaper — this sweep is the only path for them);
      * every ``klangk-net-*`` sidecar whose ``klangk.pid`` owner is no longer
        alive (the same rule as ``reap_dead_owner_containers`` in
        ``container.py``, applied standalone so a fresh klangkd need not be
        started to clear the pile-up).

    ``klangk-net-*`` sidecars with no ``klangk.pid`` (predating #2430) cannot
    be decided safe and are printed with a paste-ready ``podman rm`` line
    rather than removed automatically.
    """
    fixtures = cleanup_stale_containers()
    for name in fixtures:
        print(f"removed ctrl-dns fixture: {name}")

    names: list[str] = []
    try:
        res = subprocess.run(
            ["podman", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        names = [n for n in res.stdout.split() if n.startswith("klangk-net-")]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    dead_sidecars: list[str] = []
    legacy_sidecars: list[str] = []
    for name in names:
        try:
            r = subprocess.run(
                [
                    "podman",
                    "inspect",
                    "-f",
                    '{{index .Config.Labels "klangk.pid"}}',
                    name,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        pid = r.stdout.strip()
        try:
            owner = int(pid)
        except ValueError:
            legacy_sidecars.append(name)
            continue
        if owner > 0 and not SmokeTest._pid_alive(owner):
            dead_sidecars.append(name)

    for name in dead_sidecars:
        try:
            subprocess.run(
                ["podman", "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(f"removed dead-owner sidecar: {name}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break

    if legacy_sidecars:
        print(
            "\nlegacy label-less sidecars (pre-#2430; owner cannot be "
            "decided — remove manually if stale):"
        )
        print("  podman rm -f " + " ".join(legacy_sidecars))

    total = len(fixtures) + len(dead_sidecars)
    extra = (
        f", {len(legacy_sidecars)} legacy listed" if legacy_sidecars else ""
    )
    print(f"\ncleanup done: removed {total} container(s){extra}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
